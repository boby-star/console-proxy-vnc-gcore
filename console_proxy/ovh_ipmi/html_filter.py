import re


UI_FILTER = b"""<style>
#button_active_user,#button_help{display:none!important}
</style><script>
(function(){
  const hidden=['button_active_user','button_help'];
  function removeRestrictedControls(){
    hidden.forEach(function(id){
      const element=document.getElementById(id);
      if(element) element.remove();
    });
  }
  document.addEventListener('DOMContentLoaded',removeRestrictedControls);
  new MutationObserver(removeRestrictedControls).observe(
    document.documentElement,{childList:true,subtree:true}
  );
  removeRestrictedControls();
})();
</script>"""


def rewrite_provider_identity(body, upstream_netloc, public_host):
    """Route hard-coded provider URLs through the current public origin."""
    netloc = upstream_netloc.encode("ascii")
    absolute_url = re.compile(
        rb"(?:https?|wss?)://" + re.escape(netloc) + rb"(?P<path>/[^\"'\\\s<>]*)?",
        re.IGNORECASE,
    )

    def replace_absolute(match):
        return match.group("path") or b"/"

    body = absolute_url.sub(replace_absolute, body)
    return re.sub(
        re.escape(netloc), public_host.encode("ascii"), body, flags=re.IGNORECASE
    )


def filter_html(body, upstream_netloc, public_host):
    """Apply the minimal UI policy without changing ASRock URL semantics."""
    body = rewrite_provider_identity(body, upstream_netloc, public_host)
    lower = body.lower()
    head = lower.find(b"<head")
    if head >= 0:
        head_end = lower.find(b">", head)
        if head_end >= 0:
            return body[:head_end + 1] + UI_FILTER + body[head_end + 1:]
    body_end = lower.find(b"</body>")
    if body_end >= 0:
        return body[:body_end] + UI_FILTER + body[body_end:]
    return UI_FILTER + body