"""Storage for bot.py: SqliteStore (Docker/VPS) and DynamoStore (Lambda). Pick with STORE=sqlite|dynamo.

Users carry a `version`; save_user() writes only if it is unchanged since user() read it, else raises Conflict
and bot.py retries the turn. That replaces the old process-wide lock and works across Lambda instances."""
import os
import sqlite3
import time
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

USER_FIELDS = ("name", "state", "draft", "needs_human", "unknown_count")


class Conflict(Exception):
    """Someone else saved this user since we read it."""


def new_user(phone):
    return {"phone": phone, "name": None, "state": "idle", "draft": "{}", "needs_human": 0, "unknown_count": 0, "version": 0}


def make_store():
    return DynamoStore() if os.getenv("STORE") == "dynamo" else SqliteStore()


class SqliteStore:
    SCHEMA = """
create table if not exists users(phone text primary key, name text, state text default 'idle',
  draft text default '{}', needs_human int default 0, unknown_count int default 0, version int default 0);
create table if not exists orders(id integer primary key, phone text, name text, service text,
  location text, time_window text, status text default 'nuevo', created_at real, ready_at real,
  review_sent int default 0, confirmed_slot text);
create table if not exists unanswered(id integer primary key, phone text, text text, created_at real);
create table if not exists seen(id text primary key);  -- webhook message ids already processed
create table if not exists messages(id integer primary key, phone text, who text, text text, created_at real);
"""
    MIGRATIONS = ("alter table orders add column confirmed_slot text", "alter table users add column version int default 0")

    def q(self, sql, *args):
        # ponytail: connection per query, fine for a single laundry; pool if traffic grows.
        with closing(sqlite3.connect(os.getenv("DB_PATH", Path(__file__).parent / "lave.db"))) as c:
            c.row_factory = sqlite3.Row
            c.executescript(self.SCHEMA)
            for m in self.MIGRATIONS:  # self-heal DBs created before these columns existed
                try:
                    c.execute(m)
                except sqlite3.OperationalError:
                    pass  # duplicate column: already there
            rows = [dict(r) for r in c.execute(sql, args).fetchall()]
            c.commit()
            return rows

    def user(self, phone):
        return (self.q("select * from users where phone=?", phone) or [new_user(phone)])[0]

    def save_user(self, u):
        vals = [u[f] for f in USER_FIELDS]
        if not self.q(f"insert into users(phone, {', '.join(USER_FIELDS)}, version) values(?,?,?,?,?,?,1) "
                      f"on conflict(phone) do update set {', '.join(f'{f}=excluded.{f}' for f in USER_FIELDS)}, "
                      "version=users.version+1 where users.version=? returning phone", u["phone"], *vals, u["version"]):
            raise Conflict(u["phone"])
        u["version"] += 1

    def waiting_users(self):
        return self.q("select * from users where needs_human=1")

    def add_order(self, phone, name, service, location, time_window):
        return self.q("insert into orders(phone, name, service, location, time_window, created_at) values(?,?,?,?,?,?) "
                      "returning id", phone, name, service, location, time_window, time.time())[0]["id"]

    def orders(self, limit=200):
        return self.q("select * from orders order by id desc limit ?", limit)

    def set_status(self, order_id, status, confirmed_slot, now):
        rows = self.q("update orders set status=?, ready_at=case when ? in ('listo', 'entregado') then coalesce(ready_at, ?) "
                      "else ready_at end, confirmed_slot=coalesce(?, confirmed_slot) where id=? returning *",
                      status, status, now, confirmed_slot, order_id)
        return rows[0] if rows else None

    def claim_due_reviews(self, cutoff):
        return self.q("update orders set review_sent=1 where status in ('listo','entregado') and review_sent=0 "
                      "and ready_at<=? returning id, phone", cutoff)

    def add_unanswered(self, phone, text):
        self.q("insert into unanswered(phone, text, created_at) values(?,?,?)", phone, text, time.time())

    def unanswered(self, limit=30):
        return self.q("select * from unanswered order by id desc limit ?", limit)

    def claim_seen(self, msg_id):
        return bool(self.q("insert or ignore into seen(id) values(?) returning id", msg_id))

    def log(self, phone, who, text):
        self.q("insert into messages(phone, who, text, created_at) values(?,?,?,?)", phone, who, text, time.time())

    def messages(self, phone):
        return self.q("select * from messages where phone=? order by id", phone)

    def recent_chats(self, limit=30):
        return self.q("select m.phone, m.text, u.name, u.needs_human from messages m left join users u using(phone) "
                      "where m.id in (select max(id) from messages group by phone) order by m.id desc limit ?", limit)

    def purge_messages(self, cutoff):
        return len(self.q("delete from messages where created_at<? returning id", cutoff))


def _out(item):
    """DynamoDB Decimals back to int/float."""
    return {k: (int(v) if v % 1 == 0 else float(v)) if isinstance(v, Decimal) else v for k, v in item.items()}


def _num(x):
    return Decimal(str(x))


class DynamoStore:
    """Tables are defined in template.yaml (named <TABLE_PREFIX><users|orders|...>).
    ponytail: admin lists, recent chats and purge are table scans; fine at one shop's volume, add GSIs if it grows."""

    def __init__(self):
        import boto3
        from boto3.dynamodb.conditions import Attr, Key
        self.Attr, self.Key = Attr, Key
        ddb = boto3.resource("dynamodb")
        p = os.getenv("TABLE_PREFIX", "lave-")
        self.users, self.orders_t, self.msgs, self.unans, self.seen = (
            ddb.Table(p + n) for n in ("users", "orders", "messages", "unanswered", "seen"))
        self.Failed = ddb.meta.client.exceptions.ConditionalCheckFailedException

    def _scan(self, table, **kw):
        items, r = [], table.scan(**kw)
        items += r["Items"]
        while "LastEvaluatedKey" in r:
            r = table.scan(ExclusiveStartKey=r["LastEvaluatedKey"], **kw)
            items += r["Items"]
        return [_out(i) for i in items]

    def user(self, phone):
        item = self.users.get_item(Key={"phone": phone}, ConsistentRead=True).get("Item")
        return {**new_user(phone), **_out(item)} if item else new_user(phone)

    def save_user(self, u):
        names = {f"#{f}": f for f in (*USER_FIELDS, "version")}
        values = {f":{f}": u[f] for f in USER_FIELDS} | {":v": u["version"], ":v1": u["version"] + 1}
        try:
            self.users.update_item(
                Key={"phone": u["phone"]},
                UpdateExpression="SET " + ", ".join(f"#{f}=:{f}" for f in USER_FIELDS) + ", #version=:v1",
                ConditionExpression="attribute_not_exists(phone) OR #version = :v",
                ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        except self.Failed:
            raise Conflict(u["phone"])
        u["version"] += 1

    def waiting_users(self):
        return self._scan(self.users, FilterExpression=self.Attr("needs_human").eq(1))

    def add_order(self, phone, name, service, location, time_window):
        # Atomic counter in item id=0 keeps ids small and sequential ("pedido #12") like SQLite.
        oid = int(self.orders_t.update_item(Key={"id": 0}, UpdateExpression="ADD n :one",
                                            ExpressionAttributeValues={":one": 1}, ReturnValues="UPDATED_NEW")["Attributes"]["n"])
        self.orders_t.put_item(Item={"id": oid, "phone": phone, "name": name, "service": service, "location": location,
                                     "time_window": time_window, "status": "nuevo", "created_at": _num(time.time()),
                                     "review_sent": 0, "confirmed_slot": None})
        return oid

    def orders(self, limit=200):
        return sorted(self._scan(self.orders_t, FilterExpression=self.Attr("id").gt(0)), key=lambda o: -o["id"])[:limit]

    def set_status(self, order_id, status, confirmed_slot, now):
        expr, vals = "SET #s=:s", {":s": status}
        if status in ("listo", "entregado"):
            expr, vals[":now"] = expr + ", ready_at=if_not_exists(ready_at, :now)", _num(now)
        if confirmed_slot:
            expr, vals[":slot"] = expr + ", confirmed_slot=:slot", confirmed_slot
        try:
            r = self.orders_t.update_item(Key={"id": int(order_id)}, UpdateExpression=expr,
                                          ConditionExpression="attribute_exists(id) AND id > :zero",
                                          ExpressionAttributeNames={"#s": "status"},
                                          ExpressionAttributeValues={**vals, ":zero": 0}, ReturnValues="ALL_NEW")
        except self.Failed:
            return None
        return {"confirmed_slot": None, **_out(r["Attributes"])}

    def claim_due_reviews(self, cutoff):
        # GSI "due": review_sent (hash) + ready_at (range); only orders that reached listo/entregado have ready_at.
        r = self.orders_t.query(IndexName="due", KeyConditionExpression=self.Key("review_sent").eq(0) & self.Key("ready_at").lte(_num(cutoff)),
                                FilterExpression=self.Attr("status").is_in(["listo", "entregado"]))
        claimed = []
        for o in r["Items"]:  # claim each with a conditional write so two runs can't both send it
            try:
                self.orders_t.update_item(Key={"id": o["id"]}, UpdateExpression="SET review_sent=:one",
                                          ConditionExpression="review_sent=:zero", ExpressionAttributeValues={":one": 1, ":zero": 0})
                claimed.append({"id": int(o["id"]), "phone": o["phone"]})
            except self.Failed:
                pass
        return claimed

    def add_unanswered(self, phone, text):
        self.unans.put_item(Item={"id": uuid.uuid4().hex, "phone": phone, "text": text, "created_at": _num(time.time())})

    def unanswered(self, limit=30):
        return sorted(self._scan(self.unans), key=lambda r: -r["created_at"])[:limit]

    def claim_seen(self, msg_id):
        try:  # TTL on `expires` drops ids after a week; Meta retries for well under that
            self.seen.put_item(Item={"id": msg_id, "expires": int(time.time()) + 7 * 86400},
                               ConditionExpression="attribute_not_exists(id)")
            return True
        except self.Failed:
            return False

    def log(self, phone, who, text):
        self.msgs.put_item(Item={"phone": phone, "ts": time.time_ns(), "who": who, "text": text, "created_at": _num(time.time())})

    def messages(self, phone):
        items, kw = [], {"KeyConditionExpression": self.Key("phone").eq(phone)}
        while True:
            r = self.msgs.query(**kw)
            items += r["Items"]
            if "LastEvaluatedKey" not in r:
                return [_out(i) for i in items]
            kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def recent_chats(self, limit=30):
        last = {}
        for m in self._scan(self.msgs):
            if m["ts"] > last.get(m["phone"], {"ts": -1})["ts"]:
                last[m["phone"]] = m
        users = {u["phone"]: u for u in self._scan(self.users)}
        return [{"phone": p, "text": m["text"], "name": users.get(p, {}).get("name"), "needs_human": users.get(p, {}).get("needs_human")}
                for p, m in sorted(last.items(), key=lambda x: -x[1]["ts"])[:limit]]

    def purge_messages(self, cutoff):
        old = self._scan(self.msgs, FilterExpression=self.Attr("created_at").lt(_num(cutoff)), ProjectionExpression="phone, ts")
        with self.msgs.batch_writer() as b:
            for m in old:
                b.delete_item(Key={"phone": m["phone"], "ts": m["ts"]})
        return len(old)
