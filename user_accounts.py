"""Password accounts backed by stable mobile workspaces and revocable sessions."""
import hashlib,re,secrets,time
from ai_service import AIError as APIError
from server import password_hash,password_ok,now,uid,dumps
TTL=7*24*3600

def identity(h,c):
    token=h.cookie('golfera_user')
    if not token:return None
    row=c.execute("SELECT u.*,a.username,s.token_hash FROM user_sessions s JOIN user_accounts a ON a.user_id=s.user_id JOIN users u ON u.id=a.user_id WHERE s.token_hash=? AND s.expires>? AND u.status='active'",(hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
    if not row:raise APIError(401,'登录已失效，请重新登录。')
    return row

def public(row):return {k:row[k] for k in ('id','username','name','role','status')}

def api(h,c,path,method):
    db=h.server.database
    if path=='/api/user/me' and method=='GET':
        row=identity(h,c)
        return h.respond(200,{'user':public(row) if row else None})
    if path=='/api/user/login' and method=='POST':
        b=h.body_json();username=b.get('username','');password=b.get('password','')
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>64 or len(password)>256:raise APIError(400,'登录内容无效。')
        username=username.strip();key=('user',h.request_ip(),username)
        h.server.login_limiter.check(key)
        row=c.execute('SELECT a.*,u.status FROM user_accounts a JOIN users u ON u.id=a.user_id WHERE a.username=?',(username,)).fetchone()
        encoded=row['password_hash'] if row else 'pbkdf2_sha256$260000$unknown$'+'0'*64
        if not password_ok(password,encoded) or not row or row['status']!='active':
            h.server.login_limiter.fail(key);raise APIError(401,'账号或密码不正确，或账号已停用。')
        h.server.login_limiter.clear(key);token=secrets.token_urlsafe(32)
        with db.transaction(c):
            c.execute('DELETE FROM user_sessions WHERE expires<?',(time.time(),))
            c.execute('INSERT INTO user_sessions(token_hash,user_id,expires) VALUES(?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),row['user_id'],time.time()+TTL))
            db.audit(c,row['username'],'login','user_accounts',row['user_id'])
        h.set_cookie('golfera_user',token,TTL);h.set_cookie('golfera_visitor','',0)
        return h.respond(200,{'logged_in':True})
    if path=='/api/user/password' and method=='POST':
        user=identity(h,c)
        if not user:raise APIError(401,'请先登录。')
        b=h.body_json();old=b.get('old_password','');new=b.get('password','')
        if not isinstance(old,str) or not isinstance(new,str) or not 12<=len(new)<=256 or len(old)>256:raise APIError(400,'新密码需要12至256位。')
        key=('password',h.request_ip(),user['id']);h.server.login_limiter.check(key)
        with db.transaction(c):
            row=c.execute('SELECT password_hash FROM user_accounts WHERE user_id=?',(user['id'],)).fetchone()
            if not password_ok(old,row['password_hash']):
                h.server.login_limiter.fail(key);raise APIError(400,'当前密码不正确。')
            c.execute('UPDATE user_accounts SET password_hash=? WHERE user_id=?',(password_hash(new),user['id']))
            c.execute('DELETE FROM user_sessions WHERE user_id=?',(user['id'],))
            db.audit(c,user['username'],'password','user_accounts',user['id'])
        h.server.login_limiter.clear(key);h.set_cookie('golfera_user','',0)
        return h.respond(200,{'saved':True})
    if path=='/api/user/logout' and method=='POST':
        h.body_json();token=h.cookie('golfera_user')
        if token:
            with db.transaction(c):c.execute('DELETE FROM user_sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
        h.set_cookie('golfera_user','',0);h.set_cookie('golfera_visitor','',0)
        return h.respond(200,{'logged_out':True})
    raise APIError(404,'用户登录接口不存在。')

def admin(h,c,actor,method):
    db=h.server.database
    if method=='GET':
        rows=c.execute('SELECT a.username,u.id,u.name,u.role,u.status,u.created_at FROM user_accounts a JOIN users u ON u.id=a.user_id ORDER BY u.created_at DESC').fetchall()
        return h.respond(200,[dict(r) for r in rows])
    if method not in ('POST','PATCH'):raise APIError(405,'不支持该操作。')
    b=h.body_json();password=b.get('password','');username=b.get('username','');name=b.get('name','');status=b.get('status','active')
    if method=='POST' and (not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,64}',username) or not isinstance(name,str) or not 1<=len(name.strip())<=100):raise APIError(400,'账号需3至64位字母、数字或._-，姓名必填。')
    if not isinstance(password,str) or (method=='POST' or password) and not 12<=len(password)<=256:raise APIError(400,'密码需要12至256位。')
    if status not in ('active','inactive'):raise APIError(400,'账号状态无效。')
    with db.transaction(c):
        if method=='POST':
            user_id=uid('UA');visitor_id=secrets.token_urlsafe(24)
            db.insert(c,'users',dict(id=user_id,name=name.strip(),phone=None,role='golfer',status=status),source='admin')
            c.execute('INSERT INTO user_accounts(user_id,username,password_hash,created_at) VALUES(?,?,?,?)',(user_id,username,password_hash(password),now()))
            state={'role':'golfer','favorites':[],'joined':False,'reminders':True,'learned':[],'round':db.blank_round(),'bag':{'orderId':'','stage':0,'clubs':[False]*4,'issues':[],'records':[]}}
            c.execute('INSERT INTO visitors(id,user_id,state_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?)',(visitor_id,user_id,dumps(state),1,now(),now()))
        else:
            user_id=b.get('id')
            if not isinstance(user_id,str) or not c.execute('SELECT user_id FROM user_accounts WHERE user_id=?',(user_id,)).fetchone():raise APIError(404,'账号不存在。')
            c.execute('UPDATE users SET status=?,updated_at=? WHERE id=?',(status,now(),user_id))
            if password:c.execute('UPDATE user_accounts SET password_hash=? WHERE user_id=?',(password_hash(password),user_id))
            c.execute('DELETE FROM user_sessions WHERE user_id=?',(user_id,))
        db.audit(c,actor['username'],'create' if method=='POST' else 'update','user_accounts',user_id,{'status':status,'password_changed':bool(password)})
    return h.respond(201 if method=='POST' else 200,{'id':user_id,'saved':True})
