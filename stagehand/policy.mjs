export const BUILTIN_TOOLS = ['act','ariaTree','click','clickAndHold','dragAndDrop','extract','fillForm','fillFormVision','goto','keys','navback','screenshot','scroll','type','think','wait','search'];
export const FINAL = /^(提交|提交审核|发布|确认发布|确认提交)$/;
const RISK = /提交|发布|删除|撤回|下架|同意|免责声明|支付|购买|编辑|退出登录|切换账号|绑定账号/;
export function assertUrl(url, hosts, allowBlank=false) {
  if (allowBlank && url==='about:blank') return;
  const u=new URL(url);
  if (u.protocol!=='https:' || !hosts.includes(u.hostname)) throw Object.assign(new Error(`不在授权域名：${u.hostname}`),{code:'wrong_domain'});
}
export function safeClick(n,{final=false, navigation=false}={}) {
  if(n.disabled || n.visible===false) return false;
  if(final) return FINAL.test(n.text.trim());
  const text=[n.text,n.accessibleName].join(' ');
  const context=[n.label,n.context].join(' ');
  if(RISK.test(text)||/服务协议|用户协议|免责声明|我已阅读|授权确认/.test(context)) return false;
  if(navigation && /radio|checkbox|option/.test(n.type)) return false;
  return ['button','a','li'].includes(n.tag)||['button','menuitem'].includes(n.type)
    ||(!navigation && (['radio','checkbox','option','combobox'].includes(n.type)||['input','select','textarea'].includes(n.tag)) && !['password','file'].includes(n.type));
}
export function stateOf(s) {
  if(/验证码|安全验证|拖动.*滑块/.test(s.text)) return 'challenge';
  if(/扫码登录|扫描二维码登录|手机号登录|登录后/.test(s.text)) return 'login';
  if((s.dialogs||[]).some(t=>/协议|免责声明|我已阅读|授权确认/.test(t))) return 'agreement';
  if(/暂无权限|无访问权限|无操作权限/.test(s.text)) return 'permission_denied';
  if(/网络异常|加载失败|请求失败|服务异常/.test(s.text)) return 'page_error';
  if(s.loading) return 'loading';
  if(/版权|备案|单集信息/.test(s.text)&&s.controls.some(n=>['input','textarea','select'].includes(n.tag))) return 'metadata_form';
  if(s.controls.some(n=>n.type==='file'&&/video|视频/i.test(n.accept+n.label))) return 'video_upload';
  if(s.rows.length||s.controls.some(n=>/搜索|查询/.test(n.text+n.label))) return 'content_list';
  return 'shell';
}
export function assertPage(s) {
  const state=stateOf(s);
  const errors={challenge:'遇到验证码或安全验证，请手动处理',login:'等待扫码登录',agreement:'遇到法律协议或平台授权确认，请手动处理',permission_denied:'账号权限不足',page_error:'平台页面加载异常'};
  if(errors[state]) throw Object.assign(new Error(errors[state]),{code:state});
  return state;
}
export function signature(n) {
  return Object.fromEntries(['tag','type','text','accessibleName','label','region','frameUrl'].map(k=>[k,n[k]||'']));
}
