import json
import re


QUOTED_ROOT_URL = re.compile(rb"(?P<quote>[\"'])(?P<path>/(?!/)[^\"'\\\s<>]*)")
CSS_ROOT_URL = re.compile(rb"(?P<prefix>url\(\s*)(?P<quote>[\"']?)(?P<path>/(?!/)[^\"')\s<>]*)")


BOOTSTRAP = """<base href=__PROXY_BASE__><style id="console-proxy-ui">
#button_active_user,#button_help{display:none!important}
</style><script>
(function(){
  const proxyPrefix=__PROXY_PREFIX__;
  function proxyHttpUrl(value){
    const target=new URL(value,window.location.href);
    if(target.origin===window.location.origin && !target.pathname.startsWith(proxyPrefix+'/')){
      target.pathname=proxyPrefix+(target.pathname.startsWith('/')?'':'/')+target.pathname;
    }
    return target.toString();
  }

  const nativeFetch=window.fetch;
  if(nativeFetch){
    window.fetch=function(input,options){
      if(typeof input==='string' || input instanceof URL){
        input=proxyHttpUrl(input);
      }else if(input instanceof Request){
        input=new Request(proxyHttpUrl(input.url),input);
      }
      return nativeFetch.call(this,input,options);
    };
  }
  const nativeXhrOpen=XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open=function(method,url){
    const args=Array.prototype.slice.call(arguments,2);
    return nativeXhrOpen.call(this,method,proxyHttpUrl(url),...args);
  };

  const NativeWorker=window.Worker;
  if(NativeWorker){
    window.Worker=function(url,options){
      return new NativeWorker(proxyHttpUrl(url),options);
    };
    window.Worker.prototype=NativeWorker.prototype;
  }
  const NativeSharedWorker=window.SharedWorker;
  if(NativeSharedWorker){
    window.SharedWorker=function(url,options){
      return new NativeSharedWorker(proxyHttpUrl(url),options);
    };
    window.SharedWorker.prototype=NativeSharedWorker.prototype;
  }
  const NativeEventSource=window.EventSource;
  if(NativeEventSource){
    window.EventSource=function(url,options){
      return new NativeEventSource(proxyHttpUrl(url),options);
    };
    window.EventSource.prototype=NativeEventSource.prototype;
  }

  const NativeWebSocket=window.WebSocket;
  function ProxyWebSocket(url,protocols){
    const target=new URL(url,window.location.href);
    target.protocol=window.location.protocol==='https:'?'wss:':'ws:';
    target.host=window.location.host;
    if(!target.pathname.startsWith(proxyPrefix+'/')){
      target.pathname=proxyPrefix+(target.pathname.startsWith('/')?'':'/')+target.pathname;
    }
    return protocols===undefined?new NativeWebSocket(target):new NativeWebSocket(target,protocols);
  }
  ProxyWebSocket.prototype=NativeWebSocket.prototype;
  ['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(name){
    Object.defineProperty(ProxyWebSocket,name,{value:NativeWebSocket[name]});
  });
  window.WebSocket=ProxyWebSocket;

  const hiddenIds=['button_active_user','button_help'];
  const urlAttributes=['src','href','action','data-main'];
  function rewriteElementUrls(root){
    if(!root || root.nodeType!==Node.ELEMENT_NODE){ return; }
    const elements=[root].concat(Array.from(root.querySelectorAll('[src],[href],[action],[data-main]')));
    elements.forEach(function(element){
      urlAttributes.forEach(function(attribute){
        const value=element.getAttribute(attribute);
        if(!value || value.startsWith('#') || value.startsWith('data:') || value.startsWith('javascript:')){
          return;
        }
        element.setAttribute(attribute,proxyHttpUrl(value));
      });
    });
  }
  function maintainDocument(root){
    hiddenIds.forEach(function(id){
      const element=document.getElementById(id);
      if(element) element.remove();
    });
    rewriteElementUrls(root || document.documentElement);
  }
  document.addEventListener('DOMContentLoaded',function(){ maintainDocument(document.documentElement); });
  new MutationObserver(function(mutations){
    mutations.forEach(function(mutation){
      mutation.addedNodes.forEach(maintainDocument);
    });
  }).observe(document.documentElement,{childList:true,subtree:true});
  maintainDocument(document.documentElement);
})();
</script>"""


def rewrite_root_relative_urls(body, token):
    """Move provider root URLs below this IPMI session's public namespace."""
    prefix = f"/ipmi/{token}".encode("ascii")

    def replace_quoted(match):
        path = match.group("path")
        if path == b"/" or path == prefix or path.startswith(prefix + b"/"):
            return match.group(0)
        return match.group("quote") + prefix + path

    def replace_css(match):
        path = match.group("path")
        if path == b"/" or path == prefix or path.startswith(prefix + b"/"):
            return match.group(0)
        return match.group("prefix") + match.group("quote") + prefix + path

    body = QUOTED_ROOT_URL.sub(replace_quoted, body)
    return CSS_ROOT_URL.sub(replace_css, body)


def rewrite_provider_identity(body, token, upstream_netloc, public_host):
    """Remove the provider address from content while preserving proxy routing."""
    prefix = f"/ipmi/{token}".encode("ascii")
    netloc = upstream_netloc.encode("ascii")
    absolute_url = re.compile(
        rb"(?:https?|wss?)://" + re.escape(netloc) + rb"(?P<path>/[^\"'\\\s<>]*)?",
        re.IGNORECASE,
    )

    def replace_absolute(match):
        return prefix + (match.group("path") or b"/")

    body = absolute_url.sub(replace_absolute, body)
    return re.sub(re.escape(netloc), public_host.encode("ascii"), body, flags=re.IGNORECASE)


def filter_html(body, token, upstream_netloc, public_host):
    prefix = f"/ipmi/{token}"
    script = BOOTSTRAP.replace("__PROXY_BASE__", json.dumps(prefix + "/"))
    script = script.replace("__PROXY_PREFIX__", json.dumps(prefix))
    injected = script.encode("utf-8")
    body = rewrite_provider_identity(body, token, upstream_netloc, public_host)
    body = rewrite_root_relative_urls(body, token)
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