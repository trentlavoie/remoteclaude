"""The /files browser page: breadcrumb, sortable listing, upload widget, long-press
delete. Data only — rc_share.share_page() fills the __PLACEHOLDER__s per request."""

from rc_templates import shared

FILES_PAGE = shared("""<!doctype html>
<html><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>rc-share</title>
<style>
__THEME__
__BASE__
header{position:sticky;top:0;background:var(--bg);padding:14px 16px 10px;
border-bottom:1px solid var(--bd);z-index:2}
h1{margin:0 0 8px;font-size:13px;letter-spacing:.4px;color:var(--mut);
font-weight:600;text-transform:uppercase}
.crumb{font-size:13px;word-break:break-all;line-height:1.6}
.crumb a{color:var(--blue);text-decoration:none}
.crumb .sep{color:var(--mut);margin:0 6px}
.up{display:inline-block;margin-top:10px;font-size:12px;color:var(--blue);
border:1px solid var(--bd);border-radius:8px;padding:6px 12px;cursor:pointer;
-webkit-tap-highlight-color:transparent}
.up:active{background:var(--row2)}
.sortbar{display:flex;gap:6px;align-items:center;margin-top:10px;flex-wrap:wrap}
.sortlbl{color:var(--mut);font-size:11px;letter-spacing:.3px;text-transform:uppercase}
.sortbtn{border:1px solid var(--bd);border-radius:8px;padding:5px 11px;font:inherit;
font-size:12px;color:var(--mut);background:var(--row);cursor:pointer;
-webkit-tap-highlight-color:transparent}
.sortbtn:active{background:var(--row2)}
.sortbtn.on{color:var(--fg);border-color:var(--blue)}
li{border-radius:11px;background:var(--row);margin:6px 0}
li a{display:flex;align-items:center;gap:12px;padding:14px;color:var(--fg);
text-decoration:none;-webkit-tap-highlight-color:transparent}
li a:active{background:var(--row2)}
.nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
li.dir .nm{color:var(--accent)}
.meta{color:var(--mut);font-size:11px;white-space:nowrap;flex:0 0 auto}
li.empty{background:none;color:var(--mut);text-align:center;padding:30px}
.st{color:var(--mut);font-size:12px;margin:6px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style></head>
<body>
<header>
<h1>rc-share &middot; __HOST__</h1>
<div class=crumb>__CRUMB__</div>
<label class=up id=up>+ upload<input id=f type=file multiple hidden></label>
<div class=st id=st hidden></div>
<div class=sortbar><span class=sortlbl>sort</span><button class=sortbtn data-k=n>name</button><button class=sortbtn data-k=s>size</button><button class=sortbtn data-k=t>date</button></div>
</header>
<ul>__ROWS__</ul>
<script nonce=__NONCE__>
var REL=__REL__,up=document.getElementById('up'),st=document.getElementById('st');
// The Android wrapper tags its UA; there the WebView's DownloadManager saves the file and
// the app calls rcDownloadDone() when it finishes. Elsewhere the page fetches the bytes
// itself so it can show progress and confirm, then hands them to a download link.
var IS_APP=/rc-launcher-app/.test(navigator.userAgent),BUSY=false;
// The server serves passive types inline, so in a browser a tap on something the browser
// renders (a PDF, an image, text, video) opens it as it always did; everything else (HTML and
// SVG included — the server forces those to attachment) takes the fetch-and-confirm path. In
// the app every tap is a download.
var VIEWABLE=/\\.(pdf|txt|md|log|csv|json|png|jpe?g|gif|webp|mp4|m4a|mp3|webm)$/i;
__DOWNLOAD__
function status(m){st.hidden=!m;st.textContent=m||'';}
function statusLater(m){status(m);setTimeout(function(){status('');},4000);}
window.rcDownloadDone=function(name,ok){statusLater(ok?'\\u2713 saved '+name+' to Downloads':'\\u2717 '+name+' failed');};
async function download(a){
var name=a.querySelector('.nm').textContent,url=a.getAttribute('href');
status('\\u2b07 '+name+'\\u2026');
if(IS_APP)return;
BUSY=true;
try{var r=await fetch(url);if(!r.ok)throw new Error(r.status);
var total=+r.headers.get('Content-Length')||0;
if(!total||downloadMode(total)==='native'){await r.body.cancel();var n=document.createElement('a');n.href=url;n.download=name;document.body.appendChild(n);n.click();n.remove();
statusLater('\\u2b07 '+name+' ('+hum(total)+') handed to the browser');return;}  // too big to buffer
var rd=r.body.getReader(),chunks=[],got=0;
for(;;){var c=await rd.read();if(c.done)break;chunks.push(c.value);got+=c.value.length;
status(progressText(name,got,total));}
var b=new Blob(chunks),u=URL.createObjectURL(b),l=document.createElement('a');
l.href=u;l.download=name;document.body.appendChild(l);l.click();l.remove();
setTimeout(function(){URL.revokeObjectURL(u);},60000);
statusLater('\\u2713 downloaded '+name+' ('+hum(b.size)+')');
}catch(e){statusLater('\\u2717 '+name+' failed');}finally{BUSY=false;}}
function sleep(ms){return new Promise(function(r){setTimeout(r,ms);});}
__UPLOAD__
function putSlice(url,fl,off,end,total,rid){return new Promise(function(res){
var x=new XMLHttpRequest();x.open('PUT',url);
x.setRequestHeader('X-Rc-Offset',off);x.setRequestHeader('X-Rc-Total',total);x.setRequestHeader('X-Rc-Id',rid);
x.upload.onprogress=function(e){up.textContent=fl.name+' '+Math.floor((off+e.loaded)*100/total)+'% '+hum(off+e.loaded)+'/'+hum(total);};
x.onload=function(){res(x.status===200);};x.onerror=function(){res(false);};x.onabort=function(){res(false);};
x.send(fl.slice(off,end));});}
async function uploadFile(fl){
var url='/files'+REL+'/'+encodeURIComponent(fl.name),total=fl.size;
var rid=fl.size+'-'+fl.lastModified,idh={'X-Rc-Id':rid};
async function probe(){
try{var h=await fetch(url,{method:'HEAD',headers:idh});
if(!h.ok)return -1;
return parseInt(h.headers.get('X-Rc-Have')||'0',10)||0;}catch(e){return -1;}}
function send(off,end){return putSlice(url,fl,off,end,total,rid);}
var r=await resumeUpload(total,probe,send,
{onRetry:async function(){up.textContent=fl.name+' dropped \\u2014 retrying\\u2026';await sleep(1500);}});
if(!r.ok)up.textContent='\\u2717 '+fl.name+' failed';
return r.ok;}
document.getElementById('f').addEventListener('change',async function(e){
var fs=[].slice.call(e.target.files);if(!fs.length)return;
var ok=0,bad=0;
for(var i=0;i<fs.length;i++){if(await uploadFile(fs[i]))ok++;else bad++;}
if(!bad){location.reload();return;}
e.target.value='';
up.textContent=ok+' uploaded, '+bad+' failed \\u2014 pull to refresh';});
document.querySelectorAll('li:not(.dir):not(.empty)').forEach(function(li){
var a=li.querySelector('a');if(!a)return;var lp=false,t;
function start(){lp=false;t=setTimeout(function(){lp=true;del(a);},550);}
function cancel(){clearTimeout(t);}
a.addEventListener('contextmenu',function(e){e.preventDefault();});
a.addEventListener('touchstart',start,{passive:true});
a.addEventListener('touchend',cancel);a.addEventListener('touchmove',cancel);
a.addEventListener('mousedown',start);a.addEventListener('mouseup',cancel);
a.addEventListener('mouseleave',cancel);
a.addEventListener('click',function(e){if(lp){e.preventDefault();lp=false;return;}
if(IS_APP){download(a);return;}
if(e.metaKey||e.ctrlKey||e.shiftKey||VIEWABLE.test(a.getAttribute('href')))return;  // native
e.preventDefault();if(!BUSY)download(a);});});
async function del(a){
if(!confirm('Delete '+a.querySelector('.nm').textContent+'?'))return;
try{await fetch(a.getAttribute('href'),{method:'DELETE'});location.reload();}catch(e){}}
var UL=document.querySelector('ul');
function getSort(){try{return JSON.parse(localStorage.getItem('rc_sort'))||{k:'t',d:-1}}catch(e){return{k:'t',d:-1}}}
function applySort(){
var s=getSort(),lis=[].slice.call(UL.querySelectorAll('li[data-n]'));
lis.sort(function(a,b){
var dd=(+b.dataset.d)-(+a.dataset.d);if(dd)return dd;  // directories always on top
var c=s.k==='n'?a.dataset.n.localeCompare(b.dataset.n):(+a.dataset[s.k])-(+b.dataset[s.k]);
if(c)return c*s.d;
return a.dataset.n.localeCompare(b.dataset.n);});  // tie-break by name, always A-to-Z
lis.forEach(function(li){UL.appendChild(li);});
document.querySelectorAll('.sortbtn').forEach(function(b){var on=b.dataset.k===s.k;
b.classList.toggle('on',on);b.textContent=b.dataset.lbl+(on?(s.d>0?' \\u2191':' \\u2193'):'');});}
document.querySelectorAll('.sortbtn').forEach(function(b){b.dataset.lbl=b.textContent;
b.addEventListener('click',function(){var s=getSort();
if(s.k===b.dataset.k)s.d=-s.d;else{s.k=b.dataset.k;s.d=b.dataset.k==='n'?1:-1;}
localStorage.setItem('rc_sort',JSON.stringify(s));applySort();});});
applySort();
if(location.search)history.replaceState({},'',location.pathname);  // drop a ?token=
__PTR__
</script>
</body></html>""")
