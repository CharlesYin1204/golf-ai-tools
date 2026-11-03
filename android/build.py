#!/usr/bin/env python3
"""Build and sign a WebView Android APK using local, fixed toolchain paths."""
import os,pathlib,subprocess,tempfile,secrets,json,time,hashlib,zipfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
DATA=ROOT/'data/android';DATA.mkdir(parents=True,exist_ok=True)
TOOLS=pathlib.Path('/opt/gaoqiu-android/android-15')
JAR=pathlib.Path('/usr/lib/android-sdk/platforms/android-23/android.jar')
def run(args,**kw):
 result=subprocess.run([str(a) for a in args],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120,**kw)
 if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace')[-6000:])
password_file=DATA/'signing-password';key=DATA/'release.jks'
if not password_file.exists():
 fd=os.open(password_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(36))
env={**os.environ,'GAOQIU_SIGNING_PASSWORD':password_file.read_text()}
if not key.exists():
 run(['keytool','-genkeypair','-keystore',key,'-storepass:env','GAOQIU_SIGNING_PASSWORD','-keypass:env','GAOQIU_SIGNING_PASSWORD','-alias','gaoqiu','-keyalg','RSA','-keysize','3072','-validity','10000','-dname','CN=Gaoqiu Android, O=Gaoqiu, C=CN'],env=env);key.chmod(0o600)
code=int(time.time());version='1.0.'+time.strftime('%Y%m%d%H%M%S',time.gmtime())
with tempfile.TemporaryDirectory(prefix='gaoqiu-apk-') as temp:
 t=pathlib.Path(temp);manifest=(ROOT/'android/AndroidManifest.xml').read_text().replace('android:versionCode="1"',f'android:versionCode="{code}"').replace('android:versionName="1.0.0"',f'android:versionName="{version}"');(t/'AndroidManifest.xml').write_text(manifest)
 classes=t/'classes';classes.mkdir();dex=t/'dex';dex.mkdir()
 run(['javac','-source','8','-target','8','-bootclasspath',JAR,'-d',classes,ROOT/'android/src/cn/gaoqiu/app/MainActivity.java'])
 run(['jar','cf',t/'classes.jar','-C',classes,'.'])
 run([TOOLS/'d8','--lib',JAR,'--min-api','23','--output',dex,t/'classes.jar'])
 run([TOOLS/'aapt','package','-f','-M',t/'AndroidManifest.xml','-I',JAR,'-S',ROOT/'android/res','-F',t/'unsigned.apk'])
 with zipfile.ZipFile(t/'unsigned.apk','a') as z:z.write(dex/'classes.dex','classes.dex')
 run([TOOLS/'zipalign','-p','-f','4',t/'unsigned.apk',t/'aligned.apk'])
 target=DATA/('gaoqiu-'+str(code)+'.apk')
 run([TOOLS/'apksigner','sign','--ks',key,'--ks-key-alias','gaoqiu','--ks-pass','env:GAOQIU_SIGNING_PASSWORD','--key-pass','env:GAOQIU_SIGNING_PASSWORD','--out',target,t/'aligned.apk'],env=env)
 run([TOOLS/'apksigner','verify','--verbose',target])
 meta={'version':version,'version_code':code,'filename':target.name,'size':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'built_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'published':False}
 pending=DATA/'release.tmp';pending.write_text(json.dumps(meta));pending.replace(DATA/'release.json');print(json.dumps(meta))
