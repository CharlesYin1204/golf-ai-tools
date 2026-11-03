"""Consent-gated transient media. No raw audio/video/images are persisted."""
import base64
import hashlib
import io
import json
import math
import secrets
import time
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError
import ai_service
Error=ai_service.AIError

def decoded(value,max_size):
    if not isinstance(value,str) or len(value)>max_size*1.4:raise Error(400,'媒体内容格式错误或过大。')
    try:raw=base64.b64decode(value,validate=True)
    except (ValueError,TypeError):raise Error(400,'媒体编码无效。')
    if not raw or len(raw)>max_size:raise Error(413,'媒体文件为空或过大。')
    return raw

def transcribe(raw,mime,identity):
    c=ai_service.read()
    if not c['enabled'] or not c['api_key']:raise Error(503,'AI服务未启用。')
    ai_service.check_rate_limit(identity)
    if not ai_service.SLOTS.acquire(blocking=False):raise Error(429,'AI请求繁忙，请稍后再试。')
    start=time.monotonic()
    try:
        boundary='golf'+secrets.token_hex(16)
        ext={'audio/webm':'webm','audio/wav':'wav','audio/mp4':'m4a','audio/mpeg':'mp3','audio/ogg':'ogg'}[mime]
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{c["transcription_model"]}\r\n--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\nzh\r\n--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\njson\r\n--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="recording.{ext}"\r\nContent-Type: {mime}\r\n\r\n').encode()+raw+f'\r\n--{boundary}--\r\n'.encode()
        request=Request(c['base_url']+'/audio/transcriptions',data=body,headers={'Authorization':'Bearer '+c['api_key'],'Content-Type':'multipart/form-data; boundary='+boundary})
        try:
            with build_opener(ai_service.NoRedirect()).open(request,timeout=60) as response:result=json.loads(response.read(1024*1024))
            if not isinstance(result,dict):raise ValueError()
            text=result.get('text')
            if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError()
        except HTTPError:raise Error(502,'语音转写接口不可用或账户不支持当前转写模型，请使用文字输入。') from None
        except (URLError,TimeoutError):raise Error(504,'语音转写超时，请重试或使用文字。') from None
        except (ValueError,TypeError):raise Error(422,'未能识别清晰语音，请重新录音或输入文字。') from None
        return dict(text=text.strip(),model=c['transcription_model'],elapsed_ms=round((time.monotonic()-start)*1000))
    finally:ai_service.SLOTS.release()

def process(h,c,owner,task,b,run):
    if b.get('consent') is not True:raise Error(400,'请先同意本次媒体发送至模型服务商进行分析。')
    duration=b.get('duration')
    if type(duration) not in (float,int) or not math.isfinite(duration) or not 1<=duration<=30:raise Error(400,'请选择1至30秒的媒体。')
    if task=='transcription':
        mime=b.get('mime','')
        if not isinstance(mime,str):raise Error(400,'媒体类型无效。')
        mime=mime.split(';')[0]
        if mime not in ('audio/webm','audio/wav','audio/mp4','audio/mpeg','audio/ogg'):raise Error(400,'请选择WebM/WAV/M4A/MP3/OGG音频。')
        raw=decoded(b.get('audio'),4*1024*1024)
        signature=(raw[:4] in (b'RIFF',b'OggS',b'\x1aE\xdf\xa3') or raw[:3]==b'ID3' or raw[:2] in (b'\xff\xfb',b'\xff\xf3',b'\xff\xf2') or raw[4:8]==b'ftyp')
        if not signature:raise Error(400,'音频容器无效。')
        return run(h,c,owner,task,'语音转写',{'duration':duration,'consent_at':time.time(),'raw_retained':False},executor=lambda messages:transcribe(raw,mime,getattr(h,'request_ip',lambda:h.client_address[0])()))
    frames=b.get('frames')
    if not isinstance(frames,list) or len(frames)!=12 or duration<2:raise Error(400,'视频须至少2秒并包含12个连续时间点。')
    from PIL import Image, ImageStat, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS=1024*1024
    seen=set();images=[];timestamps=[]
    for i,frame in enumerate(frames):
        if not isinstance(frame,dict):raise Error(400,'视频帧无效。')
        t=frame.get('time')
        expected=duration*i/12
        if type(t) not in (float,int) or not math.isfinite(t) or abs(t-expected)>0.15:raise Error(400,'视频时间点不连续，请重新选择视频。')
        raw=decoded(frame.get('image'),500000)
        try:
            with Image.open(io.BytesIO(raw)) as image:
                if image.format!='JPEG' or image.width<320 or image.height<240 or image.width>960 or image.height>960:raise ValueError()
                image.load();gray=image.convert('L');stats=ImageStat.Stat(gray)
                if stats.mean[0]<12 or stats.mean[0]>245 or stats.stddev[0]<8:raise ValueError()
        except (ValueError,OSError,UnidentifiedImageError,Image.DecompressionBombError):raise Error(422,'画面过暗、过曝、分辨率不足或内容无效，请上传清晰完整挥杆视频。') from None
        seen.add(hashlib.sha256(raw).hexdigest());timestamps.append(round(t,2));images.append('data:image/jpeg;base64,'+base64.b64encode(raw).decode())
    if len(seen)<6:raise Error(422,'视频有效动作帧不足，不能用静态图片代替挥杆视频。')
    evidence=dict(duration=duration,timestamps=timestamps,consent_at=time.time(),raw_retained=False,scope='根据12个时间采样帧提供可见动作观察，不测量精确角度，不诊断病症，不承诺矫正效果。')
    def execute(messages):
        instruction='请检查这段高尔夫挥杆视频的时间采样帧。必须能看到同一人全身、球杆和完整动作，否则quality为rejected并解释。只输出JSON：{"quality":"accepted或rejected","reason":"质量说明","observations":[{"time":采样秒数,"observation":"该帧可直接看到的事实"}],"coach_questions":["可与教练核对的问题"]}。最多5项观察，不推测不可见动作，不测量精确角度，不推断受伤风险。每个time必须取自提供的时间点。'
        content=[{'type':'text','text':instruction}]
        for t,url in zip(timestamps,images):content.extend([{'type':'text','text':f'{t}秒'},{'type':'image_url','image_url':{'url':url}}])
        result=ai_service.complete([{'role':'user','content':content}],getattr(h,'request_ip',lambda:h.client_address[0])())
        try:
            text=result['text'].strip()
            if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0]
            parsed=json.loads(text)
            if not isinstance(parsed,dict):raise ValueError()
            if parsed.get('quality') not in ('accepted','rejected') or not isinstance(parsed.get('reason'),str) or not parsed['reason'].strip():raise ValueError()
            observations=parsed.get('observations',[]);questions=parsed.get('coach_questions',[])
            if not isinstance(observations,list) or len(observations)>5 or not isinstance(questions,list) or len(questions)>10:raise ValueError()
            for o in observations:
                if not isinstance(o,dict) or type(o.get('time')) not in (int,float) or o.get('time') not in timestamps or not isinstance(o.get('observation'),str) or not o['observation'].strip():raise ValueError()
            if parsed['quality']=='accepted' and not observations:raise ValueError()
            if any(not isinstance(q,str) for q in questions):raise ValueError()
        except (ValueError,TypeError,KeyError,IndexError):raise Error(502,'模型未返回可追溯的视频观察，本次不展示动作结论。') from None
        result['analysis']=parsed
        result['text']=('视频质量未通过：' if parsed['quality']=='rejected' else '视频观察（非教练诊断）：')+parsed['reason']
        if parsed['quality']=='accepted':result['text']+='\n'+'\n'.join(f"{o['time']}秒：{o['observation']}" for o in observations)+'\n与教练核对：\n'+'\n'.join(questions)
        return result
    return run(h,c,owner,'video','视频动作观察',evidence,executor=execute)
