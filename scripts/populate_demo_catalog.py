"""Explicitly invoked, idempotent sample catalogue and golfer records. Never runs at startup."""
import sys,json,os,shlex
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Database,day,now,dumps
import ai_features

def populate(db):
 with db.connection() as c:
  with db.transaction(c):
   account=c.execute("SELECT user_id FROM user_accounts WHERE username='golfer'").fetchone()
   if not account:raise RuntimeError('golfer account required')
   user=account['user_id'];v=c.execute('SELECT * FROM visitors WHERE user_id=?',(user,)).fetchone();vid=v['id'];aux=json.loads(v['state_json']);counts={}
   def add(table,data):
    if c.execute(f'SELECT 1 FROM {table} WHERE id=?',(data['id'],)).fetchone():return
    db.insert(c,table,data,source='demo-catalog');counts[table]=counts.get(table,0)+1
   courses=[]
   for i,name in enumerate(['青岚湖畔','松林山谷','云湾海岸','白鹭湿地','枫叶山麓','星河林地','南岸花园','青禾田园']):
    ident=f'DEMO_C{i+1:02}';courses.append(ident)
    add('courses',dict(id=ident,name=name+'高尔夫（示例）',short=name+'（示例）',address=['上海','苏州','杭州','宁波'][i%4]+' · 虚构场景地址',holes=18,price=680+i*120,distance=0,tags=['示例资料',['湖景球场','山地练习','海风挑战','平缓球道'][i%4],'18洞'],status='active'))
    for offset in range(15):
     for slot in ['07:30','08:30','09:30','10:30','13:00','14:00']:
      add('slots',dict(id=f'{ident}_{day(offset)}_{slot.replace(":","")}',course_id=ident,date=day(offset),time=slot,capacity=16,price=680+i*120,status='open'))
   users=[]
   for i in range(36):
    ident=f'DEMO_U{i+1:03}';users.append(ident)
    add('users',dict(id=ident,name=['林','陈','王','李','周','吴'][i%6]+f'球友{i+1:02}（示例）',phone=None,role='golfer',status='active'))
   for i in range(8):
    uid=f'DEMO_CADDIE_U{i+1}';add('users',dict(id=uid,name=f'服务员{i+1}（示例）',phone=None,role='caddie',status='active'))
    add('caddies',dict(id=f'DEMO_CD{i+1}',user_id=uid,name=f'球童{i+1}（示例）',shift='07:00—16:00' if i%2==0 else '10:00—19:00',skills=['装备清点','球洞引导','记分协助'],status=['available','busy','available','off'][i%4]))
   topics={
    '技巧':[('下场前的热身安排','把肩、髋、踝轻柔活动后，用小幅挥杆逐步找回节奏。'),('第一场球的准备清单','按球杆、球、手套、饮水与防晒用品逐项检查。'),('球场礼仪与安全观察','出杆前确认前方安全，并按现场工作人员引导行动。'),('安全落点与选杆思路','优先选择可控距离与宽阔落区，不盲目追求最大距离。'),('练习后如何记录进步','分别记录目标、结果和感受，下一次只调整一个变量。')],
    '木杆':[('一号木站位检查','先选目标，再确认杆面与身体朝向。'),('开球节奏练习','从舒适速度开始，观察收杆是否平衡。'),('球座高度观察','在教练指导下比较不同高度的触球感受。'),('球道木的稳定触球','选择平整练习位，从短幅动作感受触球。'),('开球落点分布记录','每组五球，记录方向分布而非仅关注最远一球。')],
    '铁杆':[('七号铁的基础节奏','用固定目标与稳定准备流程建立节奏。'),('短铁距离分组','选择不同目标，记录实际落点与离散范围。'),('长铁练习的目标选择','先保证安全落区，再评估距离需求。'),('铁杆触球后的平衡','练习后保持自然收杆，观察身体是否稳定。'),('铁杆距离本的建立','按球杆记录多次练习结果，不用单次最远值代表常用距离。')],
    '切杆':[('果岭边的落点选择','先观察球位、坡度及可用滚动空间。'),('低飞滚动切杆','在安全练习区比较不同落点的滚动结果。'),('不同球位的短杆决策','识别草况与障碍，优先选择简单可控的路线。'),('短杆距离阶梯','设置三个目标，每个目标练习五球并记录误差。'),('短杆复盘记录','把落点偏差与滚动距离分别记录。')],
    '推杆':[('短推准备流程','从球后观察方向，再完成站位和试挥。'),('长推距离感练习','以停球区域为目标，优先练习速度控制。'),('推杆读线观察','从多个角度观察坡度，不阻挡其他球友路线。'),('推杆节奏与平衡','保持自然节奏，观察滚动而非急于抬头。'),('减少三推的练习记录','记录第一推剩余距离，比较多组结果。')]}
   lessons=[]
   for cat,items in topics.items():
    for n,(title,focus) in enumerate(items):
     ident=f'DEMO_LESSON_{len(lessons)+1:02}';lessons.append(ident)
     add('lessons',dict(id=ident,title=title+'（示例课）',category=cat,duration=f'{5+n*2} 分钟',coach=['林教练（示例）','陈教练（示例）','王教练（示例）'][n%3],description=focus+' 本内容为图文示例，实际动作调整请结合教练指导。',steps=['准备：选择安全练习区，确认周围无人处于击球方向。','观察：'+focus,'练习：以舒适幅度完成一组五球，出现不适立即停止。',f'记录：分别写下本组{cat}练习的目标、落点和感受。','复盘：休息后比较记录，下次只改变一个因素。'],status='published'))
   for i in range(8):
    add('teams',dict(id=f'DEMO_TEAM{i+1}',name=['周末轻松约球','城市晨光','新手成长','湖畔友谊','下班练习','亲友高球','短杆研习','周日挑战'][i]+'队（示例）',captain_id=users[i],city=['上海','苏州','杭州','宁波'][i%4],member_count=12+i*3,description='示例球队，展示约球、练习与交流场景，不代表真实组织。',status='active'))
   for i in range(16):
    add('events',dict(id=f'DEMO_EVENT{i+1:02}',title=['周末友谊约球','晨间开球练习','新手下场交流','短杆专项练习'][i%4]+f' {i+1:02}（示例）',team_id=f'DEMO_TEAM{i%8+1}',course_id=courses[i%8],date=day(i+1),time='09:30',capacity=16+(i%3)*4,fee=680+(i%8)*120,description='示例活动：08:50签到，09:10规则交流，09:30分组开球，结束后交流记录。费用为示例金额，不安排真实活动或收款。',status='open'))
    for j in range(3+i%5):
     add('registrations',dict(id=f'DEMO_REG{i:02}_{j}',event_id=f'DEMO_EVENT{i+1:02}',user_id=users[(i+j)%36],people=1,status='registered'))
   own_orders=[]
   for i in range(64):
    own=i<16;owner=user if own else users[(i-16)%36];course=courses[i%8];offset=i+1 if own and i<8 else -1-i if own else i%14+1;slot='08:30';price=680+(i%8)*120;ident=f'DEMO_ORDER{i+1:03}'
    if offset<0 or offset>14:add('slots',dict(id=f'{course}_{day(offset)}_0830',course_id=course,date=day(offset),time=slot,capacity=16,price=price,status='open' if offset>0 else 'closed'))
    order=dict(id=ident,user_id=owner,course_id=course,date=day(offset),time=slot,people=1+i%3,total=price*(1+i%3),status='cancelled' if i%7==6 else 'confirmed',checked=offset<0 and i%7!=6)
    if own:order['visitor_id']=vid;own_orders.append(ident)
    add('orders',order)
   for i in range(48):
    own=i<20;scores=[4+((i+j*3)%4) for j in range(18)]
    record=dict(id=f'DEMO_ROUND{i+1:03}',user_id=user if own else users[i%36],course_id=courses[i%8],date=day(-2-i*2),tee=['蓝Tee','白Tee','红Tee'][i%3],players=['我'],pk='练习球局',scores=scores,complete=True,total=sum(scores),hole=17,pending=False)
    if own:record['visitor_id']=vid
    add('rounds',record)
   for i in range(8,14):
    for j,action in enumerate(['领取球包','归还球包']):
     add('bag_records',dict(id=f'DEMO_BAG{i}_{j}',visitor_id=vid,user_id=user,order_id=own_orders[i],bag_code=f'DEMO-B{i:03}',action=action,clubs=[True]*4,staff=f'示例服务员{i%8+1}',record_time=day(-1-i)+('T08:00:00+08:00' if j==0 else 'T14:00:00+08:00'),notes='用户授权添加的示例交接，不代表真实服务'))
   for i in range(16):
    own=i<8
    ticket=dict(id=f'DEMO_TICKET{i+1:02}',order_id=own_orders[i] if own else f'DEMO_ORDER{i+17:03}',user_id=user if own else users[i%36],caddie_id=None if i%3==0 else f'DEMO_CD{i%8+1}',type=['装备清点','饮水补给','开球时间咨询','球包标牌核对'][i%4],description=['示例：球友希望核对球杆数量。','示例：球友询问饮水补给位置。','示例：请工作人员核对预约时段。','示例：球包标牌信息需再次确认。'][i%4],priority='high' if i%5==0 else 'normal',status='open' if i%3 else 'resolved',reported_time=now())
    if own:ticket['visitor_id']=vid
    else:
     row=c.execute('SELECT user_id FROM orders WHERE id=?',(ticket['order_id'],)).fetchone();ticket['user_id']=row['user_id']
    add('service_tickets',ticket)
   event=db.event_and_team(c)['event']
   if event and not c.execute("SELECT 1 FROM registrations WHERE user_id=? AND event_id=?",(user,event['id'])).fetchone():add('registrations',dict(id='DEMO_GOLFER_EVENT_JOIN',visitor_id=vid,user_id=user,event_id=event['id'],people=1,status='registered'))
   aux['favorites']=list(dict.fromkeys(aux.get('favorites',[])+courses[:5]));aux['learned']=list(dict.fromkeys(aux.get('learned',[])+lessons[:8]))
   c.execute('UPDATE visitors SET state_json=?,revision=revision+1,updated_at=? WHERE id=?',(dumps(aux),now(),vid))
   admin=c.execute('SELECT id FROM admins LIMIT 1').fetchone();admin_owner='admin:'+admin['id']
   for i in range(6):
    rid=f'DEMO_ROUND{i+1:03}';context,version=ai_features.round_context(c,vid,rid);ident=f'DEMO_AI_PERSONAL{i+1}'
    if not c.execute('SELECT 1 FROM ai_records WHERE id=?',(ident,)).fetchone():ai_features.put(c,vid,'run',dict(task='report' if i%2==0 else 'training',status='示例',model='示例内容（未调用模型）',text=f'示例报告 {i+1}：本场记录了18洞，共{context["statistics"]["total"]}杆。练习时可先关注记分完整性，逐步补充推杆、罚杆和落点。此内容用于展示页面，不是模型生成或专业诊断。',evidence=context,source_version=version,sources=[],elapsed_ms=0),ident)
   for i in range(3):
    ident=f'DEMO_AI_ADMIN{i+1}'
    if not c.execute('SELECT 1 FROM ai_records WHERE id=?',(ident,)).fetchone():ai_features.put(c,admin_owner,'run',dict(task='operations',status='示例',model='示例内容（未调用模型）',text=['示例经营复盘：观察预约时段分布、取消记录与服务工单，所有金额为示例，不代表到账收入。','示例服务复盘：优先核查未分配工单，再安排具备对应技能的服务人员。','示例教学复盘：按分类查看已发布课程，收集学习反馈后再调整内容。'][i],evidence={'data_type':'demo','executed':False},sources=[],elapsed_ms=0),ident)
   for i,title in enumerate(['球场签到流程示例','装备交接说明示例','活动取消规则草稿','教学内容审核示例']):
    ident=f'DEMO_KNOWLEDGE{i+1}'
    if not c.execute('SELECT 1 FROM ai_records WHERE id=?',(ident,)).fetchone():ai_features.put(c,admin_owner,'knowledge',dict(title=title,content='这是一条待审核的示例资料。请替换为真实业务规则并核对来源后再发布。',source='示例资料，待核实',keywords=['示例'],status='draft',audience='all',course_id='',valid_from=day(),valid_until=day(90),topic=title,version=1,reviewer='',reviewed_at=''),ident)
   db.audit(c,'authorized-maintenance','add-demo','database','catalog',{'counts':counts,'requested_by_user':True})
   result={'added':counts,'totals':{t:c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ['courses','slots','lessons','teams','events','orders','rounds','users','caddies','service_tickets','bag_records']},'golfer':{'orders':c.execute('SELECT COUNT(*) FROM orders WHERE user_id=?',(user,)).fetchone()[0],'history':c.execute('SELECT COUNT(*) FROM rounds WHERE user_id=? AND complete=1',(user,)).fetchone()[0]}}
   return result
if __name__=='__main__':
 for line in Path('/etc/gaoqiu.env').read_text().splitlines():
  if '=' in line and not line.startswith('#'):
   k,v=line.split('=',1);os.environ[k]=shlex.split(v)[0]
 print(json.dumps(populate(Database(backend='mysql',initialize=False)),ensure_ascii=False))
