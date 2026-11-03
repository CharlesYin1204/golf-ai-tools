"""Admin APK build/publish and public same-origin downloads."""
import io,json,pathlib,threading,subprocess,sys
from ai_service import AIError
ROOT=pathlib.Path(__file__).resolve().parent
DATA=ROOT/'data/android';LOCK=threading.Lock();BUILDING=False;ERROR=''
def info():
    try:d=json.loads((DATA/'release.json').read_text())
    except FileNotFoundError:d={'published':False}
    return {**d,'building':BUILDING,'error':ERROR,'download_url':'/api/public/app/apk'}
def binary(h,body,kind,filename=None):
    h.send_response(200)
    if filename:h.send_header('Content-Disposition','attachment; filename="'+filename+'"')
    h.headers_common(kind,len(body))
    if h.command!='HEAD':h.wfile.write(body)
def public(h,method,path):
    if method!='GET':raise AIError(405,'只支持读取。')
    d=info()
    if path=='/api/public/app':return h.respond(200,{k:v for k,v in d.items() if k not in ('filename','error')})
    if not d.get('published') or not d.get('filename'):raise AIError(404,'安装包暂未开放下载。')
    if path=='/api/public/app/qr':
        import qrcode
        buf=io.BytesIO();qrcode.make('https://gaoqiu.yinchao1204.vip/api/public/app/apk').save(buf,format='PNG');return binary(h,buf.getvalue(),'image/png')
    if path=='/api/public/app/apk':
        target=DATA/d['filename']
        if target.parent!=DATA or not target.is_file():raise AIError(404,'安装包不存在。')
        return binary(h,target.read_bytes(),'application/vnd.android.package-archive',d['filename'])
    raise AIError(404,'下载地址不存在。')
def build():
    global BUILDING,ERROR
    try:
        result=subprocess.run([sys.executable,str(ROOT/'android/build.py')],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=240)
        if result.returncode:
            DATA.mkdir(parents=True,exist_ok=True);(DATA/'build-error.log').write_bytes(result.stderr);ERROR='安装包生成失败，请联系运维查看构建日志。'
    except Exception:ERROR='构建超时或环境未准备好，请联系运维。'
    finally:
        with LOCK:BUILDING=False

def admin(h,c,actor,path,method):
    global BUILDING,ERROR
    if path=='/api/admin/app' and method=='GET':return h.respond(200,info())
    if path=='/api/admin/app/build' and method=='POST':
        h.body_json()
        with LOCK:
            if BUILDING:raise AIError(409,'已有生成任务正在运行。')
            BUILDING=True;ERROR=''
        with h.server.database.transaction(c):h.server.database.audit(c,actor['username'],'build','app_release','android')
        threading.Thread(target=build,daemon=True).start()
        return h.respond(202,{'building':True})
    if path=='/api/admin/app' and method=='PATCH':
        b=h.body_json()
        if type(b.get('published'))is not bool:raise AIError(400,'发布状态无效。')
        with LOCK:
            if BUILDING:raise AIError(409,'生成期间不能更改发布状态。')
            d=info()
            if not d.get('filename'):raise AIError(409,'请先生成APK。')
            d={k:v for k,v in d.items() if k not in ('building','error','download_url')};d['published']=b['published']
            t=DATA/'publish.tmp';t.write_text(json.dumps(d));t.replace(DATA/'release.json')
        with h.server.database.transaction(c):h.server.database.audit(c,actor['username'],'publish','app_release',d['version'],{'published':d['published']})
        return h.respond(200,info())
    raise AIError(404,'App管理接口不存在。')
