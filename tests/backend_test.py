#!/usr/bin/env python3
"""使用真实 HTTP 请求与临时 SQLite 的集成回归，无第三方测试依赖。

运行：python3 sites/golfera/tests/backend_test.py
测试不使用部署数据库，不发送真实预约、消息或支付。
"""
import copy
import importlib.util
import json
import os
import secrets
import socket
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
from urllib.parse import quote, urlsplit

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("golfera_server",ROOT/"server.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NoRedirectHandler(HTTPRedirectHandler):
    """保留真实重定向响应，以验收入口地址与 Location，不让客户端掩盖问题。"""
    def redirect_request(self,request,fp,code,message,headers,new_url):
        return None


class AdminAssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets=[]

    def handle_starttag(self,tag,attributes):
        attrs=dict(attributes)
        if tag=="link" and attrs.get("rel")=="stylesheet":
            self.assets.append((attrs.get("href",""),"text/css"))
        elif tag=="script" and attrs.get("src"):
            self.assets.append((attrs["src"],"javascript"))


class Client:
    def __init__(self,base):
        self.base=base
        self.cookies=CookieJar()
        self.opener=build_opener(HTTPCookieProcessor(self.cookies))

    def request(self,path,method="GET",data=None,origin=None,follow_redirects=True):
        headers={"Content-Type":"application/json"}
        if method!="GET":
            headers["Origin"]=origin or self.base
        payload=json.dumps(data if data is not None else {}).encode() if method!="GET" else None
        request=Request(self.base+quote(path,safe="/?:=&%"),data=payload,method=method,headers=headers)
        opener=self.opener if follow_redirects else build_opener(HTTPCookieProcessor(self.cookies),NoRedirectHandler())
        try:
            response=opener.open(request,timeout=30)
        except HTTPError as error:
            response=error
        body=response.read()
        content_type=response.headers.get("Content-Type","")
        parsed=json.loads(body) if "application/json" in content_type else body.decode("utf-8") if content_type.startswith("text/") or "javascript" in content_type else body
        return response.status,parsed,response.headers

    def data(self,path,method="GET",data=None,expected=200):
        status,body,headers=self.request(path,method,data)
        if status!=expected or not body.get("ok"):
            raise AssertionError(f"{method} {path}: {status} {body}")
        return body["data"]


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix="golfera-backend-test-")
        cls.db_path=Path(cls.temp.name)/"golfera.sqlite3"
        cls.password="TestOnly_"+secrets.token_urlsafe(16)
        original=os.environ.get("GOLFERA_ADMIN_PASSWORD")
        os.environ["GOLFERA_ADMIN_PASSWORD"]=cls.password
        cls.db=module.Database(cls.db_path,test_fixtures=True)
        if original is None:
            del os.environ["GOLFERA_ADMIN_PASSWORD"]
        else:
            os.environ["GOLFERA_ADMIN_PASSWORD"]=original
        cls.http=module.GolfServer(("127.0.0.1",0),cls.db,ROOT/"dist")
        cls.thread=threading.Thread(target=cls.http.serve_forever,daemon=True)
        cls.thread.start()
        cls.base=f"http://127.0.0.1:{cls.http.server_port}"
        cls.admin=Client(cls.base)
        cls.admin.data("/api/auth/login","POST",{"username":"admin","password":cls.password})

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def mobile(self):
        client=Client(self.base)
        result=client.data("/api/mobile/state")
        return client,result["state"]

    def save(self,client,state):
        return client.data("/api/mobile/state","PUT",{"state":state})["state"]

    def test_01_health_static_schema_seed(self):
        self.assertTrue(Client(self.base).data("/api/health")["ready"])
        for path in ("/","/app.js","/data.js","/assets/course.jpg"):
            status,body,headers=Client(self.base).request(path)
            self.assertEqual(status,200,path)
            self.assertEqual(headers["X-Content-Type-Options"],"nosniff")
        schema=self.admin.data("/api/admin/schema")
        self.assertEqual(len(schema["models"]),13)
        self.assertFalse(schema["models"]["audit_logs"]["editable"])
        expected={"courses":3,"slots":270,"users":37,"caddies":6,"orders":15,"rounds":12,"teams":3,"events":3,"registrations":8,"service_tickets":4,"bag_records":6,"lessons":6}
        for model,count in expected.items():
            listing=self.admin.data(f"/api/admin/{model}?page_size=200")
            self.assertEqual(listing["total"],count,model)
            self.assertTrue(listing["items"])
        course=self.admin.data("/api/admin/courses/qizhong")
        self.admin.data("/api/admin/courses/qizhong","PATCH",{"short":"旗忠测试保留"})
        result=self.admin.data("/api/admin/seed","POST",{})
        self.assertEqual(result["inserted"],0)
        self.assertEqual(self.admin.data("/api/admin/courses/qizhong")["short"],"旗忠测试保留")
        self.admin.data("/api/admin/courses/qizhong","PATCH",{"short":course["short"]})
        courses=Client(self.base).data("/api/public/courses")
        self.assertTrue(isinstance(courses[0]["slots"][0],str))
        self.assertTrue(isinstance(courses[0]["tags"],list))

    def test_02_auth_csrf_and_validation(self):
        anonymous=Client(self.base)
        for model in module.MODELS:
            self.assertEqual(anonymous.request("/api/admin/"+model)[0],401)
        self.assertEqual(anonymous.request("/api/auth/login","POST",{"username":"admin","password":"wrong"})[0],401)
        self.assertEqual(anonymous.request("/api/auth/login","POST",{"username":"admin","password":self.password},origin="https://evil.example")[0],403)
        self.assertEqual(self.admin.request("/api/admin/audit_logs","POST",{})[0],403)
        self.assertEqual(self.admin.request("/api/admin/users","POST",{"name":"错误手机号","phone":"abc","role":"golfer","status":"active"})[0],400)
        self.assertEqual(self.admin.request("/api/admin/orders","POST",{"user_id":"missing","course_id":"qizhong","date":module.day(2),"time":"08:00","people":2})[0],400)
        self.assertEqual(self.admin.request("/api/admin/settings","PATCH",{"password":"not allowed"})[0],400)
        self.assertEqual(anonymous.request("/%2e%2e/server.py")[0],403)

    def test_03_crud_search_export_settings_dashboard(self):
        user=self.admin.data("/api/admin/users","POST",{"name":"集成测试球友","phone":"13811112222","role":"golfer","status":"active"},201)
        ident=user["id"]
        self.assertEqual(self.admin.data("/api/admin/users?q=集成测试")["items"][0]["id"],ident)
        edited=self.admin.data(f"/api/admin/users/{ident}","PATCH",{"name":"集成测试已编辑"})
        self.assertEqual(edited["name"],"集成测试已编辑")
        self.assertEqual(self.admin.data(f"/api/admin/users/{ident}")["name"],edited["name"])
        status,csv,headers=self.admin.request("/api/admin/export/users")
        self.assertEqual(status,200)
        self.assertIn("集成测试已编辑",csv)
        self.assertIn("attachment",headers["Content-Disposition"])
        self.admin.data(f"/api/admin/users/{ident}","DELETE",{})
        self.assertEqual(self.admin.request(f"/api/admin/users/{ident}")[0],404)
        settings=self.admin.data("/api/admin/settings")
        self.admin.data("/api/admin/settings","PATCH",{"site_name":"高尔夫AI工具演示测试"})
        self.assertEqual(self.admin.data("/api/admin/settings")["site_name"],"高尔夫AI工具演示测试")
        self.admin.data("/api/admin/settings","PATCH",{"site_name":settings["site_name"]})
        dashboard=self.admin.data("/api/admin/dashboard")
        self.assertEqual(len(dashboard["trend"]),14)
        self.assertTrue(dashboard["stats"])
        self.assertTrue(dashboard["recent_orders"])
        self.assertTrue(dashboard["service_alerts"])

    def test_04_concurrent_inventory_cancellation(self):
        course=self.admin.data("/api/admin/courses","POST",{"id":"TEST_INVENTORY","name":"并发验证球场","short":"测试","address":"模拟地址","holes":18,"price":125,"distance":0,"tags":[],"status":"active"},201)
        slot=self.admin.data("/api/admin/slots","POST",{"course_id":course["id"],"date":module.day(10),"time":"15:30","capacity":2,"price":125,"status":"open"},201)
        data={"course_id":course["id"],"date":module.day(10),"time":"15:30","people":2,"status":"confirmed","checked":False}
        def book(n):
            client=Client(self.base)
            client.data("/api/auth/login","POST",{"username":"admin","password":self.password})
            return client.request("/api/admin/orders","POST",{**data,"user_id":f"U{n:03d}"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(book,[25,26]))
        self.assertEqual(sorted(result[0] for result in results),[201,409])
        order=next(result[1]["data"] for result in results if result[0]==201)
        slot_read=self.admin.data("/api/admin/slots/"+slot["id"])
        self.assertEqual(slot_read["booked"],2)
        self.assertEqual(slot_read["available"],0)
        self.assertEqual(self.admin.request("/api/admin/slots/"+slot["id"],"PATCH",{"capacity":1})[0],409)
        self.assertEqual(self.admin.request("/api/admin/slots/"+slot["id"],"DELETE",{})[0],409)
        self.admin.data("/api/admin/orders/"+order["id"],"PATCH",{"status":"cancelled"})
        self.assertEqual(self.admin.data("/api/admin/slots/"+slot["id"])["available"],2)
        second=self.admin.data("/api/admin/orders","POST",{**data,"user_id":"U027"},201)
        self.assertEqual(second["total"],250)
        for ident in (order["id"],second["id"]):
            self.admin.data("/api/admin/orders/"+ident,"DELETE",{})
        self.admin.data("/api/admin/slots/"+slot["id"],"DELETE",{})
        self.admin.data("/api/admin/courses/"+course["id"],"DELETE",{})

    def test_05_mobile_isolation_booking_admin_sync_rounds(self):
        client,state=self.mobile()
        other,other_state=self.mobile()
        self.assertNotEqual(state["orders"][0]["id"],other_state["orders"][0]["id"])
        new_id="GETEST"+secrets.token_hex(8)
        state["orders"].append({"id":new_id,"course":"yintao","date":module.day(3),"slot":"13:00","people":3,"total":1,"status":"confirmed","checked":False})
        state=self.save(client,state)
        record=self.admin.data("/api/admin/orders/"+new_id)
        self.assertEqual(record["total"],2640)
        self.assertEqual(record["source"],"visitor")
        old=copy.deepcopy(state)
        self.admin.data("/api/admin/orders/"+new_id,"PATCH",{"checked":True})
        self.assertEqual(client.request("/api/mobile/state","PUT",{"state":old})[0],409)
        state=client.data("/api/mobile/state")["state"]
        self.assertTrue(next(item for item in state["orders"] if item["id"]==new_id)["checked"])
        malicious=copy.deepcopy(other_state)
        malicious["orders"][0]["id"]=new_id
        self.assertEqual(other.request("/api/mobile/state","PUT",{"state":malicious})[0],403)
        invalid=copy.deepcopy(state)
        invalid["round"]["scores"][0]=21
        self.assertEqual(client.request("/api/mobile/state","PUT",{"state":invalid})[0],400)
        self.assertEqual(client.data("/api/mobile/state")["state"]["round"]["scores"][0],5)
        state["round"]["scores"]=module.SAMPLE_SCORES[:]
        state["round"]["complete"]=True
        state["history"].insert(0,copy.deepcopy(state["round"]))
        ident=state["round"]["id"]
        state=self.save(client,state)
        self.assertEqual(sum(item["id"]==ident for item in state["history"]),1)
        self.assertEqual(self.admin.data("/api/admin/rounds/"+ident)["total"],86)
        self.assertEqual(self.admin.data("/api/admin/rounds?q="+ident)["total"],1)
        state["joined"]=True
        state=self.save(client,state)
        self.assertTrue(state["joined"])
        state["joined"]=False
        state=self.save(client,state)
        self.assertFalse(state["joined"])

    def test_06_bag_tickets_admin_resolution(self):
        client,state=self.mobile()
        order_id=state["bag"]["orderId"]
        invalid=copy.deepcopy(state)
        invalid["bag"]["stage"]=1
        invalid["bag"]["records"]=[{"action":"领取球包","time":"测试"}]
        self.assertEqual(client.request("/api/mobile/state","PUT",{"state":invalid})[0],409)
        self.admin.data("/api/admin/orders/"+order_id,"PATCH",{"checked":True})
        state=client.data("/api/mobile/state")["state"]
        state["bag"]["issues"]=[{"type":"装备数量差异","text":"模拟清点发现少一支推杆","time":"09:12","closed":False}]
        state=self.save(client,state)
        issue_id=state["bag"]["issues"][0]["id"]
        invalid=copy.deepcopy(state)
        invalid["bag"]["stage"]=1
        invalid["bag"]["clubs"]=[False]*4
        invalid["bag"]["records"].append({"action":"领取球包","time":"测试"})
        self.assertEqual(client.request("/api/mobile/state","PUT",{"state":invalid})[0],409)
        self.admin.data("/api/admin/service_tickets/"+issue_id,"PATCH",{"status":"resolved"})
        state=client.data("/api/mobile/state")["state"]
        self.assertTrue(state["bag"]["issues"][0]["closed"])
        state["bag"]["stage"]=1
        state["bag"]["records"].append({"action":"领取球包","time":"测试"})
        state=self.save(client,state)
        self.assertEqual(state["bag"]["stage"],1)
        cannot_cancel=copy.deepcopy(state)
        next(item for item in cannot_cancel["orders"] if item["id"]==order_id)["status"]="cancelled"
        self.assertEqual(client.request("/api/mobile/state","PUT",{"state":cannot_cancel})[0],409)
        state["bag"]["clubs"]=[True]*4
        state["bag"]["stage"]=2
        state["bag"]["records"].append({"action":"归还球包","time":"测试"})
        state=self.save(client,state)
        self.assertEqual(state["bag"]["stage"],2)
        records=self.admin.data("/api/admin/bag_records?q="+order_id)
        self.assertEqual(records["total"],2)
        self.assertEqual({item["action"] for item in records["items"]},{"领取球包","归还球包"})
        result=client.data("/api/mobile/reset","POST",{})
        self.assertEqual(result["state"]["bag"]["stage"],0)
        self.assertEqual(self.admin.data("/api/admin/orders/"+order_id)["status"],"cancelled")

    def test_07_database_reopen_and_password_storage(self):
        client,state=self.mobile()
        order_id=state["orders"][0]["id"]
        reopened=module.Database(self.db_path)
        with reopened.connection() as conn:
            self.assertEqual(conn.execute("SELECT people FROM orders WHERE id=?",(order_id,)).fetchone()[0],2)
            encoded=conn.execute("SELECT password_hash FROM admins").fetchone()[0]
            self.assertTrue(encoded.startswith("pbkdf2_sha256$"))
            self.assertNotIn(self.password,encoded)
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0],1)
            self.assertTrue(conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]>0)

    def test_08_login_rate_limit_logout(self):
        limited=Client(self.base)
        for n in range(8):
            self.assertEqual(limited.request("/api/auth/login","POST",{"username":"limit_test","password":"wrong"})[0],401)
        self.assertEqual(limited.request("/api/auth/login","POST",{"username":"limit_test","password":"wrong"})[0],429)
        account=Client(self.base)
        result=account.request("/api/auth/login","POST",{"username":"admin","password":self.password})
        self.assertEqual(result[0],200)
        self.assertIn("HttpOnly",result[2]["Set-Cookie"])
        self.assertIn("SameSite=Lax",result[2]["Set-Cookie"])
        self.assertNotIn("password",str(result[1]))
        account.data("/api/auth/logout","POST",{})
        self.assertEqual(account.request("/api/auth/me")[0],401)

    def test_09_every_editable_model_crud_and_relational_search(self):
        created=[]
        def create(model,data):
            result=self.admin.data("/api/admin/"+model,"POST",data,201)
            created.append((model,result["id"]))
            return result
        user=create("users",{"name":"全模块测试球友","role":"golfer","status":"active"})
        caddie_user=create("users",{"name":"全模块测试球童","role":"caddie","status":"active"})
        caddie=create("caddies",{"user_id":caddie_user["id"],"name":"测试球童","shift":"09:00—17:00","skills":["清点"],"status":"available"})
        self.admin.data("/api/admin/caddies/"+caddie["id"],"PATCH",{"shift":"10:00—18:00"})
        round_value=create("rounds",{"user_id":user["id"],"course_id":"qizhong","date":module.day(),"tee":"白Tee","players":["我"],"pk":"练习球局","scores":[None]*18,"complete":False})
        complete=self.admin.data("/api/admin/rounds/"+round_value["id"],"PATCH",{"scores":module.SAMPLE_SCORES,"complete":True})
        self.assertEqual(complete["total"],86)
        team=create("teams",{"name":"全模块测试球队","captain_id":user["id"],"city":"上海","member_count":2,"description":"模拟测试","status":"active"})
        event=create("events",{"title":"全模块测试活动","team_id":team["id"],"course_id":"qizhong","date":module.day(8),"time":"13:00","capacity":2,"fee":980,"description":"模拟约球","status":"open"})
        registration=create("registrations",{"event_id":event["id"],"user_id":user["id"],"people":1,"status":"registered"})
        self.assertEqual(self.admin.data("/api/admin/events/"+event["id"])["registered"],1)
        self.assertEqual(self.admin.data("/api/admin/registrations?q=全模块测试活动")["items"][0]["id"],registration["id"])
        self.assertEqual(self.admin.request("/api/admin/registrations","POST",{"event_id":event["id"],"user_id":user["id"],"people":1,"status":"registered"})[0],409)
        self.admin.data("/api/admin/registrations/"+registration["id"],"PATCH",{"status":"cancelled"})
        self.assertEqual(self.admin.data("/api/admin/events/"+event["id"])["registered"],0)
        order=create("orders",{"user_id":user["id"],"course_id":"qizhong","date":module.day(8),"time":"13:00","people":1,"status":"confirmed","checked":True})
        self.assertTrue(any(item["id"]==order["id"] for item in self.admin.data("/api/admin/orders?q=全模块测试球友")["items"]))
        self.assertTrue(self.admin.data("/api/admin/orders?q=旗忠")["total"]>0)
        ticket=create("service_tickets",{"order_id":order["id"],"user_id":user["id"],"caddie_id":caddie["id"],"type":"客人服务需求","description":"需要补充一瓶饮用水","priority":"normal","status":"open"})
        self.admin.data("/api/admin/service_tickets/"+ticket["id"],"PATCH",{"status":"resolved"})
        bags=[]
        for action in ("领取球包","归还球包"):
            bags.append(create("bag_records",{"order_id":order["id"],"user_id":user["id"],"bag_code":"B-TEST","action":action,"clubs":[True]*4,"staff":"测试球童","record_time":module.now(),"notes":"模拟交接"}))
        self.admin.data("/api/admin/bag_records/"+bags[-1]["id"],"PATCH",{"notes":"双向确认已完成"})
        lesson=create("lessons",{"title":"全模块测试教学","category":"推杆","duration":"3 分钟","coach":"测试教练","description":"保持舒适节奏","steps":["观察方向","稳定速度"],"status":"published"})
        self.assertTrue(any(item["id"]==lesson["id"] for item in Client(self.base).data("/api/mobile/state")["lessons"]))
        self.admin.data("/api/admin/lessons/"+lesson["id"],"PATCH",{"status":"draft"})
        # 先删业务子记录，再删关联主资料；每个删除都实际读取确认。
        priorities={"bag_records":0,"service_tickets":0,"registrations":0,"orders":1,"rounds":1,"events":1,"caddies":1,"lessons":1,"teams":2,"users":3}
        for model,ident in sorted(created,key=lambda item:priorities[item[0]]):
            self.admin.data(f"/api/admin/{model}/{ident}","DELETE",{})
            self.assertEqual(self.admin.request(f"/api/admin/{model}/{ident}")[0],404)

    def test_10_admin_deleted_seed_record_stays_deleted_on_reopen(self):
        self.admin.data("/api/admin/lessons/strategy","DELETE",{})
        reopened=module.Database(self.db_path)
        with reopened.connection() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM lessons WHERE id='strategy'").fetchone())
        # 只有明确点击“补齐模拟数据”才会恢复被删的种子资料。
        result=self.admin.data("/api/admin/seed","POST",{})
        self.assertEqual(result["inserted"],1)

    def test_11_disabled_course_retained_for_old_orders(self):
        client,state=self.mobile()
        self.admin.data("/api/admin/courses/qizhong","PATCH",{"status":"inactive"})
        result=client.data("/api/mobile/state")
        self.assertTrue(any(item["id"]=="qizhong" and item["status"]=="inactive" for item in result["courses"]))
        self.assertFalse(any(item["id"]=="qizhong" for item in Client(self.base).data("/api/public/courses")))
        self.assertTrue(any(item["course"]=="qizhong" for item in result["state"]["orders"]))
        self.admin.data("/api/admin/courses/qizhong","PATCH",{"status":"active"})

    def test_12_event_team_admin_updates_and_live_registration_count(self):
        client,state=self.mobile()
        original=self.admin.data("/api/admin/events/event_weekend")
        other_events=[self.admin.data("/api/admin/events/"+key) for key in ("E002","E003")]
        for event in other_events:self.admin.data("/api/admin/events/"+event["id"],"PATCH",{"date":module.day(20)})
        team=self.admin.data("/api/admin/teams/"+original["team_id"])
        self.admin.data("/api/admin/events/event_weekend","PATCH",{"title":"后台修改后的活动","date":module.day(6),"capacity":12,"fee":1280,"description":"后台修改后的集合安排"})
        self.admin.data("/api/admin/teams/"+team["id"],"PATCH",{"name":"后台修改后的球队"})
        result=client.data("/api/mobile/state")
        self.assertEqual(result["event"]["title"],"后台修改后的活动")
        self.assertEqual(result["event"]["date"],module.day(6))
        self.assertEqual(result["event"]["fee"],1280)
        self.assertEqual(result["team"]["name"],"后台修改后的球队")
        before=result["event"]["registered"]
        state=result["state"]
        state["joined"]=True
        saved=client.data("/api/mobile/state","PUT",{"state":state})
        self.assertTrue(saved["state"]["joined"])
        self.assertEqual(saved["event"]["registered"],before+1)
        state=saved["state"]
        state["joined"]=False
        saved=client.data("/api/mobile/state","PUT",{"state":state})
        self.assertEqual(saved["event"]["registered"],before)
        for event in other_events:self.admin.data("/api/admin/events/"+event["id"],"PATCH",{"date":event["date"]})
        # 恢复原资料，不覆盖审计和报名记录。
        self.admin.data("/api/admin/events/event_weekend","PATCH",{key:original[key] for key in ("title","date","capacity","fee","description")})
        self.admin.data("/api/admin/teams/"+team["id"],"PATCH",{"name":team["name"]})

    def test_13_admin_directory_redirect_and_absolute_assets(self):
        client=Client(self.base)
        asset_urls={}
        def verify_html(html,path):
            parser=AdminAssetParser()
            parser.feed(html)
            paths={urlsplit(url).path for url,content_type in parser.assets}
            self.assertIn("/admin/admin.css",paths,path)
            self.assertIn("/admin/admin.js",paths,path)
            for url,content_type in parser.assets:
                parsed=urlsplit(url)
                self.assertFalse(parsed.scheme or parsed.netloc,path)
                self.assertTrue(parsed.path.startswith("/admin/"),path)
                asset_urls[url]=content_type
        for path,location in (("/admin","/admin/"),("/admin?from=demo&tab=orders","/admin/?from=demo&tab=orders")):
            status,body,headers=client.request(path,follow_redirects=False)
            self.assertIn(status,(302,308),path)
            self.assertEqual(headers.get("Location"),location,path)
            followed_status,html,followed_headers=client.request(path)
            self.assertEqual(followed_status,200,path)
            self.assertIn("text/html",followed_headers.get("Content-Type",""))
            verify_html(html,path)
        for path in ("/admin/","/admin/index.html"):
            status,html,headers=client.request(path)
            self.assertEqual(status,200,path)
            self.assertIn("text/html",headers.get("Content-Type",""))
            verify_html(html,path)
        # 逐个读取 HTML 实际引用的完整 URL（含版本 query），包括启动兜底脚本。
        for path,content_type in asset_urls.items():
            status,body,headers=client.request(path)
            self.assertEqual(status,200,path)
            self.assertIn(content_type,headers.get("Content-Type",""),path)
            self.assertTrue(body,path)

    def test_14_malformed_request_logging_has_no_missing_path(self):
        # parse_request 尚未建立 path 的坏请求也必须能写日志并返回400。
        with self.assertLogs("golfera",level="INFO") as captured:
            with socket.create_connection(("127.0.0.1",self.http.server_port),timeout=5) as connection:
                connection.sendall(b"BOGUS\r\n")
                connection.shutdown(socket.SHUT_WR)
                response=connection.recv(8192)
            self.assertTrue(response)
            self.assertIn(b"400",response)
        self.assertTrue(captured.output)
        self.assertFalse(any("AttributeError" in line for line in captured.output))
        self.assertTrue(Client(self.base).data("/api/health")["ready"])

    def test_99_password_rotation_and_other_session_invalidation(self):
        other=Client(self.base)
        other.data("/api/auth/login","POST",{"username":"admin","password":self.password})
        new="Rotated_"+secrets.token_urlsafe(18)
        self.admin.data("/api/auth/password","PATCH",{"old_password":self.password,"new_password":new})
        self.assertEqual(other.request("/api/auth/me")[0],401)
        self.assertEqual(Client(self.base).request("/api/auth/login","POST",{"username":"admin","password":self.password})[0],401)
        Client(self.base).data("/api/auth/login","POST",{"username":"admin","password":new})
        self.assertEqual(self.admin.data("/api/auth/me")["user"]["username"],"admin")


if __name__=="__main__":
    unittest.main(verbosity=2)
