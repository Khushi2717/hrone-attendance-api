"""A small, deliberately STRICT interpreter for exactly the aggregation operators app/main.py uses.

!! This is a SIMULATION written from the MongoDB documentation. It is NOT MongoDB. !!
It lets the pipeline *logic* (field names, stage order, null-vs-missing handling, window frames, unwinds, the
union/group gap filling) run end to end here, but it cannot prove server-side syntax acceptance, concurrency,
index use or performance. Unknown operators raise NotImplementedError; type mismatches raise instead of coercing.
$group output order is shuffled on purpose because MongoDB gives no ordering guarantee there."""
import copy
import math
import random
from datetime import datetime, timedelta, timezone


class _Missing:
    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False


MISSING = _Missing()
_SHUFFLE = random.Random(20261011)


def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def truthy(v):
    if v is MISSING or v is None:
        return False
    if isinstance(v, bool):
        return v
    return v != 0 if is_num(v) else True


def _walk(cur, parts):
    if not parts:
        return cur
    if isinstance(cur, list):
        return [x for x in (_walk(i, parts) for i in cur) if x is not MISSING]
    if isinstance(cur, dict) and parts[0] in cur:
        return _walk(cur[parts[0]], parts[1:])
    return MISSING


def get_path(doc, path):
    return _walk(doc, path.split("."))


def _eq(a, b):
    if a is MISSING or b is MISSING:  # aggregation $eq: missing is not equal to null
        return a is MISSING and b is MISSING
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _cmp(a, b, op):
    same = (is_num(a) and is_num(b)) or (isinstance(a, str) and isinstance(b, str)) or \
           (isinstance(a, datetime) and isinstance(b, datetime))
    if not same:
        raise TypeError(f"simulator: refusing to order {a!r} vs {b!r}")
    return {"$gt": a > b, "$gte": a >= b, "$lt": a < b, "$lte": a <= b}[op]


def _nullish(*vals):
    return any(v is None or v is MISSING for v in vals)


def _args(args, doc, vars):
    return [ev(a, doc, vars) for a in args]


def op_cond(args, doc, vars):
    if isinstance(args, dict):
        c, a, b = args["if"], args["then"], args["else"]
    else:
        c, a, b = args
    return ev(a, doc, vars) if truthy(ev(c, doc, vars)) else ev(b, doc, vars)


def op_in(args, doc, vars):
    x, arr = _args(args, doc, vars)
    if not isinstance(arr, list):
        raise TypeError("$in needs an array")
    return any(_eq(x, y) for y in arr)


def op_ifnull(args, doc, vars):
    v = ev(args[0], doc, vars)
    return ev(args[1], doc, vars) if _nullish(v) else v


def op_add(args, doc, vars):
    vals = _args(args, doc, vars)
    if _nullish(*vals):
        return None
    dates = [v for v in vals if isinstance(v, datetime)]
    if dates:
        return dates[0] + timedelta(milliseconds=sum(v for v in vals if is_num(v)))
    return sum(vals)


def op_multiply(args, doc, vars):
    vals = _args(args, doc, vars)
    if _nullish(*vals):
        return None
    out = 1
    for v in vals:
        out *= v
    return out


def op_divide(args, doc, vars):
    a, b = _args(args, doc, vars)
    if _nullish(a, b):
        return None
    return a / b  # ZeroDivisionError propagates, as MongoDB errors too


def op_floor(arg, doc, vars):
    v = ev(arg, doc, vars)
    return None if _nullish(v) else float(math.floor(v))


def op_round(args, doc, vars):
    v, place = _args(args, doc, vars)
    if place != 0:
        raise NotImplementedError("only $round to 0 places is simulated")
    return None if _nullish(v) else float(round(v))  # MongoDB $round is half-to-even, like Python's round


def op_tolong(arg, doc, vars):
    v = ev(arg, doc, vars)
    return None if _nullish(v) else int(v)


def op_dayofweek(arg, doc, vars):
    d = ev(arg, doc, vars)
    return None if _nullish(d) else (d.weekday() + 1) % 7 + 1  # 1 = Sunday


def op_datefromstring(args, doc, vars):
    s = ev(args["dateString"], doc, vars)
    if _nullish(s):
        return None
    try:
        return datetime.strptime(s, args["format"]).replace(tzinfo=timezone.utc)
    except ValueError:
        if "onError" in args:
            return ev(args["onError"], doc, vars)
        raise


def op_datetostring(args, doc, vars):
    d = ev(args["date"], doc, vars)
    return None if _nullish(d) else d.strftime(args["format"])


def op_range(args, doc, vars):
    a, b = _args(args, doc, vars)
    return list(range(a, b))


def op_arrayelemat(args, doc, vars):
    arr, i = _args(args, doc, vars)
    if _nullish(arr):
        return None
    return arr[i] if -len(arr) <= i < len(arr) else MISSING


OPS = {
    "$literal": lambda a, d, v: a,
    "$cond": op_cond, "$in": op_in, "$ifNull": op_ifnull,
    "$eq": lambda a, d, v: _eq(*_args(a, d, v)),
    "$gt": lambda a, d, v: _cmp(*_args(a, d, v), "$gt"), "$gte": lambda a, d, v: _cmp(*_args(a, d, v), "$gte"),
    "$lt": lambda a, d, v: _cmp(*_args(a, d, v), "$lt"), "$lte": lambda a, d, v: _cmp(*_args(a, d, v), "$lte"),
    "$and": lambda a, d, v: all(truthy(x) for x in _args(a, d, v)),
    "$add": op_add, "$multiply": op_multiply, "$divide": op_divide, "$floor": op_floor, "$round": op_round,
    "$toLong": op_tolong, "$isNumber": lambda a, d, v: is_num(ev(a, d, v)),
    "$dayOfWeek": op_dayofweek, "$dateFromString": op_datefromstring, "$dateToString": op_datetostring,
    "$range": op_range, "$arrayElemAt": op_arrayelemat,
}


def ev(e, doc, vars):
    if isinstance(e, str):
        if e.startswith("$$"):
            name, *rest = e[2:].split(".")
            if name != "ROOT" and name not in vars:
                raise KeyError(f"undefined variable ${name}")
            return _walk(doc if name == "ROOT" else vars[name], rest)
        return get_path(doc, e[1:]) if e.startswith("$") else e
    if isinstance(e, list):
        return [ev(x, doc, vars) for x in e]
    if isinstance(e, dict):
        first = next(iter(e), None)
        if isinstance(first, str) and first.startswith("$"):
            if first not in OPS:
                raise NotImplementedError(f"operator {first} is not simulated")
            return OPS[first](e[first], doc, vars)
        return {k: ev(x, doc, vars) for k, x in e.items()}
    return e


# ------------------------------------------------------------------ $match
def _match_cond(val, cond):
    if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
        for op, arg in cond.items():
            if op == "$exists":
                ok = (val is not MISSING) == bool(arg)
            elif op == "$size":
                ok = isinstance(val, list) and len(val) == arg
            elif op in ("$gt", "$gte", "$lt", "$lte"):
                try:
                    ok = False if _nullish(val) else _cmp(val, arg, op)
                except TypeError:
                    ok = False  # query comparison is type-bracketed: other types simply do not match
            else:
                raise NotImplementedError(f"query operator {op} is not simulated")
            if not ok:
                return False
        return True
    if cond is None:
        return val is MISSING or val is None
    return _eq(val, cond)


def match_doc(q, doc, vars):
    for k, cond in q.items():
        if k == "$expr":
            if not truthy(ev(cond, doc, vars)):
                return False
        elif k == "$or":
            if not any(match_doc(s, doc, vars) for s in cond):
                return False
        elif not _match_cond(get_path(doc, k), cond):
            return False
    return True


# ------------------------------------------------------------------ stages
def st_match(spec, docs, db, vars):
    return [d for d in docs if match_doc(spec, d, vars)]


def st_group(spec, docs, db, vars):
    groups = {}
    for d in docs:
        key = ev(spec["_id"], d, vars)
        key = None if key is MISSING else key
        g = groups.setdefault(repr(key), {"_id": key, **{f: [] for f in spec if f != "_id"}})
        for f, acc in spec.items():
            if f == "_id":
                continue
            ((op, arg),) = acc.items()
            if op != "$sum":
                raise NotImplementedError(f"accumulator {op} is not simulated")
            g[f].append(ev(arg, d, vars))
    out = [{"_id": g["_id"], **{f: sum(x for x in vals if is_num(x)) for f, vals in g.items() if f != "_id"}}
           for g in groups.values()]
    _SHUFFLE.shuffle(out)
    return out


def st_lookup(spec, docs, db, vars):
    foreign, out = db(spec["from"]), []
    for d in docs:
        if "localField" in spec:
            lv = get_path(d, spec["localField"])
            lv = None if lv is MISSING else lv
            matched = [f for f in foreign if (lambda fv: (None if fv is MISSING else fv) == lv)(get_path(f, spec["foreignField"]))]
        else:
            matched = foreign
        sub_vars = {**vars, **{k: ev(x, d, vars) for k, x in spec.get("let", {}).items()}}
        res = run(spec["pipeline"], matched, db, sub_vars) if "pipeline" in spec else copy.deepcopy(matched)
        out.append({**d, spec["as"]: res})
    return out


def st_unwind(spec, docs, db, vars):
    path, preserve = (spec, False) if isinstance(spec, str) else (spec["path"], spec.get("preserveNullAndEmptyArrays", False))
    field = path[1:]
    assert "." not in field, "simulator only unwinds top-level fields"
    out = []
    for d in docs:
        v = d.get(field, MISSING)
        if isinstance(v, list) and v:
            out += [{**d, field: item} for item in v]
        elif isinstance(v, list):
            if preserve:
                out.append({k: x for k, x in d.items() if k != field})
        elif _nullish(v):
            if preserve:
                out.append(d)
        else:
            out.append(d)
    return out


def st_unionwith(spec, docs, db, vars):
    return docs + run(spec["pipeline"], db(spec["coll"]), db, vars)


def st_set(spec, docs, db, vars):
    out = []
    for d in docs:
        nd = dict(d)
        for k, e in spec.items():
            v = ev(e, d, vars)  # every expression sees the INPUT document, as in MongoDB
            if v is not MISSING:
                nd[k] = v
        out.append(nd)
    return out


def st_project(spec, docs, db, vars):
    inclusion = any(v not in (0, False) for k, v in spec.items() if k != "_id")
    out = []
    for d in docs:
        if not inclusion:
            out.append({k: x for k, x in d.items() if spec.get(k, 1) not in (0, False)})
            continue
        nd = {}
        if "_id" not in spec and "_id" in d:
            nd["_id"] = d["_id"]
        for k, v in spec.items():
            if k == "_id" and v in (0, False):
                continue
            if isinstance(v, (int, bool)) and v in (1, True):
                if k in d:
                    nd[k] = d[k]
            else:
                val = ev(v, d, vars)
                if val is not MISSING:
                    nd[k] = val
        out.append(nd)
    return out


def _rank_key(v):
    return (0, 0) if _nullish(v) else (1, v) if is_num(v) else (2, v) if isinstance(v, str) else (3, repr(v))


def sort_docs(spec, docs):
    out = list(docs)
    for k, direction in reversed(list(spec.items())):
        out.sort(key=lambda d: _rank_key(get_path(d, k)), reverse=direction == -1)
    return out


def st_sort(spec, docs, db, vars):
    return sort_docs(spec, docs)


def st_limit(spec, docs, db, vars):
    return docs[:spec]


def st_count(spec, docs, db, vars):
    return [{spec: len(docs)}] if docs else []


def st_setwindowfields(spec, docs, db, vars):
    ((key, direction),) = spec["sortBy"].items()
    docs = sort_docs(spec["sortBy"], docs)
    keys = [get_path(d, key) for d in docs]
    out = [dict(d) for d in docs]
    for field, expr in spec["output"].items():
        if "$rank" in expr:
            first_with_key = {}
            for i, kv in enumerate(keys):
                first_with_key.setdefault(repr(kv), i)
            for i, kv in enumerate(keys):
                out[i][field] = first_with_key[repr(kv)] + 1   # competition ranking: 1, 2, 2, 4
        else:
            ((op, arg),) = [(o, a) for o, a in expr.items() if o != "window"]
            lo, hi = expr["window"]["documents"]
            if op != "$sum":
                raise NotImplementedError(f"window accumulator {op} is not simulated")
            for i in range(len(docs)):
                frame = docs[max(0, i + lo): min(len(docs) - 1, i + hi) + 1]
                out[i][field] = sum(v for v in (ev(arg, f, vars) for f in frame) if is_num(v))
    return out


STAGES = {"$match": st_match, "$group": st_group, "$lookup": st_lookup, "$unwind": st_unwind,
          "$unionWith": st_unionwith, "$set": st_set, "$addFields": st_set, "$project": st_project,
          "$sort": st_sort, "$limit": st_limit, "$count": st_count, "$setWindowFields": st_setwindowfields}


def run(pipeline, docs, db, vars=None):
    docs = [copy.deepcopy(d) for d in docs]
    for stage in pipeline:
        ((name, spec),) = stage.items()
        if name not in STAGES:
            raise NotImplementedError(f"stage {name} is not simulated")
        docs = STAGES[name](spec, docs, db, vars or {})
    return docs


# ------------------------------------------------------------------ database wrapper
class SimColl:
    """mongomock collection whose aggregate() runs through the simulator."""

    def __init__(self, coll, simdb):
        self._c, self._db = coll, simdb

    def aggregate(self, pipeline, **_kw):
        return iter(run(pipeline, list(self._c.find({})), self._db.docs))

    def __getattr__(self, name):
        return getattr(self._c, name)


class SimDB:
    def __init__(self, mongomock_db):
        self._d = mongomock_db

    def docs(self, name):
        return list(self._d[name].find({}))

    def __getattr__(self, name):
        return SimColl(self._d[name], self)

    def __getitem__(self, name):
        return SimColl(self._d[name], self)

    def command(self, *_a, **_k):
        raise NotImplementedError("explain needs a real MongoDB")
