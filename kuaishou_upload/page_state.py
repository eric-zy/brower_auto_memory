"""Classification uses visible evidence; it never treats a click as success."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PageState:
    name: str
    evidence: str = ''


class PageBlocked(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def classify_page(snap):
    text = snap.get('text', '')
    controls = snap.get('controls', [])
    if re.search(r'拖动.*滑块|安全验证|请完成.*验证|验证码', text):
        return PageState('challenge', '页面要求安全验证')
    if re.search(r'扫码登录|扫描二维码登录|手机号登录|登录后', text):
        return PageState('login', '页面要求登录')
    dialogs = ' '.join(snap.get('dialogs', []))
    if re.search(r'服务协议|用户协议|免责声明|我已阅读|授权协议', dialogs):
        return PageState('agreement', '存在待确认协议')
    if re.search(r'暂无权限|无访问权限|没有权限|无操作权限|403\b', text):
        return PageState('permission_denied', '页面明确报告权限不足')
    if re.search(r'系统异常|服务异常|网络异常|加载失败|请求失败|服务器错误', text):
        return PageState('page_error', '页面明确报告加载或服务错误')
    if snap.get('loading'):
        return PageState('loading', '存在可见加载状态')
    if re.search(r'不能修改当前内容的资料', text):
        return PageState('locked', '审核锁定')
    if any(n.get('type') == 'file' and re.search(r'video|视频', n.get('accept', '') + n.get('label', ''), re.I) for n in controls):
        return PageState('video_upload', '存在视频文件控件')
    if re.search(r'版权|备案|单集信息', text) and any(n.get('tag') in {'input', 'textarea', 'select'} for n in controls):
        return PageState('metadata_form', '存在资料表单字段')
    if snap.get('rows') or any(re.search(r'搜索|查询', n.get('text', '') + n.get('label', '')) for n in controls):
        return PageState('content_list', '存在列表或查询控件')
    if any(re.search(r'内容上传|上传短剧|上传内容|上传作品', n.get('text', '') + n.get('accessibleName', '')) for n in controls):
        return PageState('upload_entry', '存在上传入口')
    if snap.get('dialogs'):
        return PageState('dialog', dialogs[:180])
    if re.search(r'付费短剧|内容管理', text):
        return PageState('shell', '只有导航，缺少可确认的业务内容')
    return PageState('unknown', '未取得阶段证据')
