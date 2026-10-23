"""运行 python main.py，扫描目录并自动处理未上传视频。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from kuaishou_upload.config import PROJECT, Settings
from kuaishou_upload.database import DONE, FINISHED, UploadDatabase
from kuaishou_upload.ledger import Ledger, sync_ledger
from kuaishou_upload.scan import scan_directory
from kuaishou_upload.state import atomic_json, exclusive_lock


async def process_queue(settings, database, report):
    # 没有可处理任务时，不导入浏览器、不检查密钥、不发起 API 调用。
    from browser_use import Agent, ChatDeepSeek, ChatOpenAI
    from kuaishou_upload.tools import UploadTask, build_tools
    from kuaishou_upload.tools import ScanParams
    from kuaishou_upload.diagnostics import RunTrace
    from kuaishou_upload.llm import ReliableModel
    from kuaishou_upload.recovery import ModelLocatorRecovery
    from kuaishou_upload.platforms import get_adapter

    class QueueAgent(Agent):
        async def close(self):
            # run() 结束后还需由程序等待上传、截图；统一在本任务 finally 清理。
            pass

    model = os.getenv("OPENAI_MODEL", "deepseek-flash-free")
    connection = {"model": model, "api_key": os.environ["OPENAI_API_KEY"],
                  "base_url": os.environ["OPENAI_BASE_URL"], "timeout": settings.request_timeout,
                  "temperature": 0.1, "max_tokens": 1024}
    adapter = get_adapter(settings.platform)
    skill = (PROJECT / adapter.skill_path).read_text(encoding="utf-8")
    if settings.use_vision and 'deepseek' in model.lower():
        raise ValueError('当前DeepSeek配置不支持图像输入；请更换支持视觉的模型或关闭UPLOAD_USE_VISION')
    summary = report.summary()
    results = []
    locked_series = set()
    ordered = report.ordered_jobs()
    if settings.max_jobs == 1:
        # 首次联调优先跑第1集或已有作品，避免一上来用缺少作品的第3集测试新建入口。
        ordered.sort(key=lambda pair: not (pair[1].manifest.episodes[0].number == 1 or database.get(pair[0])["content_id"]))
    for sha256, job in ordered:
        if settings.max_jobs and len(results) >= settings.max_jobs:
            print(f"已达到本轮 {settings.max_jobs} 集上限，剩余任务保留待处理。", flush=True)
            break
        if database.get(sha256)["status"] in DONE:
            continue
        context = UploadTask(job, sha256, settings, database, summary)
        trace = RunTrace(settings.runtime_root / "logs" / context.job_id, secrets=[connection["api_key"]])
        context.trace = trace
        llm = agent = None
        def get_model():
            nonlocal llm
            if llm is None:
                inner = ChatDeepSeek(**connection, client_params={"max_retries": 0}) if 'deepseek' in model.lower() else ChatOpenAI(**connection, max_retries=0)
                llm = ReliableModel(inner, trace, timeout=settings.request_timeout)
            return llm
        context.browser.recover = ModelLocatorRecovery(get_model)
        context.browser.use_vision = settings.use_vision
        async def browser_event(event):
            trace.event('browser', eventType=event.get('kind'), **{k: v for k, v in event.items() if k != 'kind'})
            database.append_event(sha256, event)
        context.browser.event_sink = browser_event
        if context.record["series_key"] in locked_series:
            database.update(sha256, status="blocked", message="本轮已确认同剧审核中或已完结，跳过", ledger_dirty=1)
            continue
        print(f"\n[处理] {job.manifest.seriesTitle} 第{job.manifest.episodes[0].number}集：{job.files['video'].name}", flush=True)
        try:
            await context.set_stage("启动浏览器")
            await context.open()
            await context.set_stage("检查登录")
            try:
                await context.start()
            except RuntimeError as exc:
                if not any(word in str(exc) for word in ("登录", "验证码", "安全验证")) or settings.headless:
                    raise
                print("请在已打开的浏览器中完成登录/验证，然后回到终端按 Enter 继续。", flush=True)
                await asyncio.to_thread(input)
                await context.start()
            await context.bootstrap()
            if context.record["status"] not in FINISHED and not context.reconcile_only and not context.awaiting_upload:
                await context.set_stage("模型处理特殊控件")
                llm = get_model()
                initial = await context.scan_directory(ScanParams())
                agent = QueueAgent(
                    task=(f"当前任务ID={context.job_id}。程序已处理入口、查重及可确定的字段。仅处理剩余特殊控件；不要重复入口或已选附件。"
                          "调用 inspect_platform 获取当前表单引用，fill_form 处理特殊字段，其余常规填表和上传会自动继续。"
                          "handoff_to_program=true 时交回程序等待上传；test_prepared 表示模拟准备完成、未提交。"
                          "阻塞时明确说明，完成后 done。当前资料：" + json.dumps(initial["current_job"], ensure_ascii=False)),
                    llm=llm, browser=context.browser.browser, tools=build_tools(context), use_vision=settings.use_vision,
                    extend_system_message=skill, flash_mode=True, use_thinking=False, enable_planning=False,
                    max_actions_per_step=1, max_failures=1, final_response_after_failure=False, use_judge=False,
                    llm_timeout=int(settings.request_timeout * 2 + 5), step_timeout=int(settings.request_timeout * 2 + 15),
                    max_history_items=4, max_clickable_elements_length=6000,
                    save_conversation_path=str(trace.folder / "conversations"), save_conversation_path_encoding="utf-8",
                    available_file_paths=[str(path) for path in job.files.values()],
                )
                async def before_step(_agent):
                    await context.set_stage("模型处理特殊控件")
                history = await agent.run(max_steps=settings.max_steps, on_step_start=before_step)
                errors = [str(error) for error in history.errors() if error] if callable(getattr(history, "errors", None)) else []
                trace.event("agent_finished", errors=errors)
                if llm.failed:
                    raise RuntimeError(llm.last_error or "模型请求失败，停止本轮队列")
                if errors and context.record["status"] not in FINISHED and not context.awaiting_upload:
                    raise RuntimeError(errors[-1])
            if context.awaiting_upload and context.record["status"] not in FINISHED:
                await context.wait_for_upload()
            if context.record["status"] not in FINISHED | {"blocked", "unknown"}:
                missing_files = set(job.files) - set(context.state.get("uploadedAssets", []))
                await context.fail(RuntimeError("流程未完成；剩余字段：" + ("、".join(context.remaining_fields()) or "无") +
                                                "；待选附件：" + ("、".join(sorted(missing_files)) or "无")))
        except KeyboardInterrupt:
            await context.fail(RuntimeError("用户中断，提交结果不明确时下次仅核对"))
            raise
        except Exception as exc:
            await context.fail(exc)
        finally:
            if agent and callable(getattr(Agent, "close", None)):
                try:
                    await Agent.close(agent)
                except Exception as exc:
                    trace.event("agent_cleanup_error", message=str(exc))
            try:
                await context.browser.close()
            except Exception as exc:
                trace.event("browser_cleanup_error", message=str(exc))
        result = context.status()
        results.append(result)
        atomic_json(settings.runtime_root / "last_run.json", {"scan": summary["counts"], "results": results})
        print(f"[{result['status']}] {job.files['video'].name}：{result['message']}", flush=True)
        if "审核中" in result["message"] or "已完结" in result["message"]:
            locked_series.add(context.record["series_key"])
        for message in sync_ledger(database, Ledger(settings.ledger)):
            print(f"[台账待同步] {message}；上传状态已保存，下次自动同步。", flush=True)
        if llm and llm.failed:
            print(f"模型生成已失败，停止本轮；后续任务不继续消耗调用。日志：{trace.folder}", flush=True)
            break
        if context.requires_user:
            print("需要人工处理登录或验证码，本轮停止，后续视频保持待处理。", flush=True)
            break
    return results


def main(argv=None):
    load_dotenv(PROJECT / ".env")
    parser = argparse.ArgumentParser(description="扫描 E:\\upload_test，自动上传未发布视频到快手；已上传内容不重复提交")
    parser.add_argument("--dry-run", action="store_true", help="只扫描预览，不改台账或数据库，不启动浏览器或调用模型")
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    from kuaishou_upload.platforms import get_adapter
    get_adapter(settings.platform)
    os.environ['BROWSER_USE_CONFIG_DIR'] = str(settings.runtime_root / 'browser-config')
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    os.environ.setdefault("BROWSER_USE_CLOUD_SYNC", "false")
    with exclusive_lock(settings.runtime_root / "upload.lock"):
        database = UploadDatabase(settings.root / "uploads.sqlite3", preview=args.dry_run,
                                  platform=settings.platform, account_id=settings.account_id)
        try:
            if not args.dry_run:
                for message in sync_ledger(database, Ledger(settings.ledger)):
                    print(f"[台账待同步] {message}")
            report = scan_directory(settings, database)
            summary = report.summary()
            atomic_json(settings.runtime_root / "last_scan.json", summary)
            checklist = settings.runtime_root / "待补资料.md"
            checklist.write_text(report.checklist(settings.directory), encoding="utf-8")
            print(f"扫描目录：{settings.directory}\n统计：{summary['counts']}", flush=True)
            if summary.get("config"):
                print(f"公共固定配置：{summary['config']}\n自动生成/更新单集 JSON：{summary['generated']} 份", flush=True)
            if summary["test_jobs"]:
                print(f"模拟调试任务：{summary['test_jobs']} 个；会打开浏览器、填表和上传附件，在最终提交前停止。", flush=True)
            print(f"本轮处理上限：{settings.max_jobs or '全部'}；UPLOAD_MAX_JOBS=0 可恢复批量。", flush=True)
            groups = {}
            for item in report.items:
                if item["status"] != "pending":
                    groups.setdefault((item["status"], item.get("reason", "")), []).append(item)
            for (status, reason), items in groups.items():
                reason = reason.replace("\n", "；")
                if len(reason) > 180:
                    reason = reason[:180] + "…（完整原因见 last_scan.json）"
                relative = Path(items[0]["video"]).relative_to(settings.directory)
                label = str(relative) if len(items) == 1 else f"{len(items)} 个视频（如 {relative}）"
                print(f"[{status}] {label}：{reason}")
            if args.dry_run:
                print(f"仅预览，未上传。中文清单：{checklist}\n完整报告：{settings.root / 'last_scan.json'}")
                return 0
            results = []
            if report.jobs:
                if os.getenv("OPENAI_API_KEY", "").strip() in {"", "your-api-key-here"} or not os.getenv("OPENAI_BASE_URL", "").strip():
                    raise ValueError("存在待处理视频，请先在 .env 填写 OPENAI_API_KEY 和 OPENAI_BASE_URL")
                results = asyncio.run(process_queue(settings, database, report))
                atomic_json(settings.runtime_root / "last_run.json", {"scan": summary["counts"], "results": results})
            else:
                print("没有待处理任务；已上传或已完成模拟准备的内容已跳过，其他原因见扫描报告。")
            errors = sync_ledger(database, Ledger(settings.ledger))
            for message in errors:
                print(f"[台账待同步] {message}；数据库已保留，下次自动同步。")
            outstanding = any(result["status"] not in FINISHED for result in results)
            print(f"本轮结束。数据库：{database.path}\n台账：{settings.ledger}")
            return 1 if errors or outstanding or any(item["status"] == "blocked" for item in report.items) else 0
        finally:
            database.close()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("已停止。已提交和结果待核对记录保留，下次不会直接重复提交。")
        sys.exit(130)
    except Exception as exc:
        print(f"运行失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        sys.exit(1)
