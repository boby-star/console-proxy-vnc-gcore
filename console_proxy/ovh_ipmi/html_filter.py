import json


BOOTSTRAP = """<style id="console-ipmi-ui">
#button_active_user,#button_help{display:none!important}
</style><script>
(function(){
  const proxyPrefix=__PROXY_PREFIX__;
  const upstreamHost=__UPSTREAM_HOST__;
  const NativeWebSocket=window.WebSocket;
  function ProxyWebSocket(url,protocols){
    const target=new URL(url,window.location.href);
    if((target.protocol==='ws:' || target.protocol==='wss:') &&
       (target.hostname===upstreamHost || target.host===window.location.host)){
      target.protocol=window.location.protocol==='https:'?'wss:':'ws:';
      target.host=window.location.host;
      if(!target.pathname.startsWith(proxyPrefix+'/')){
        target.pathname=proxyPrefix+(target.pathname.startsWith('/')?'':'/')+target.pathname;
      }
    }
    return protocols===undefined?new NativeWebSocket(target):new NativeWebSocket(target,protocols);
  }
  ProxyWebSocket.prototype=NativeWebSocket.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(name){
    Object.defineProperty(ProxyWebSocket,name,{value:NativeWebSocket[name]});
  });
  window.WebSocket=ProxyWebSocket;

  const hiddenIds=['button_active_user','button_help'];
  function removeHiddenControls(){
    hiddenIds.forEach(function(id){
      const element=document.getElementById(id);
      if(element) element.remove();
    });
  }
  document.addEventListener('DOMContentLoaded',removeHiddenControls);
  new MutationObserver(removeHiddenControls).observe(document.documentElement,{childList:true,subtree:true});
  removeHiddenControls();
})();
</script>"""


def filter_html(body, token, upstream_hostname):
    script = BOOTSTRAP.replace("__PROXY_PREFIX__", json.dumps(f"/ipmi/{token}"))
    script = script.replace("__UPSTREAM_HOST__", json.dumps(upstream_hostname))
    injected = script.encode("utf-8")
    lower = body.lower()
    head = lower.find(b"<head")
    if head >= 0:
        head_end = lower.find(b">", head)
        if head_end >= 0:
            return body[:head_end + 1] + injected + body[head_end + 1:]
    body_end = lower.find(b"</body>")
    if body_end >= 0:
        return body[:body_end] + injected + body[body_end:]
    return injected + body