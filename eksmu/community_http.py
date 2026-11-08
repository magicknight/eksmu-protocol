"""Small HTTP routing adapter for Community; authentication stays in the server."""
from urllib.parse import parse_qs, urlsplit
from .store import Problem, exact


def get(hub, raw_path, actor_id, research=None, analysis=None):
    url = urlsplit(raw_path)
    query = parse_qs(url.query, keep_blank_values=True)
    if any(len(values) != 1 for values in query.values()):
        raise Problem("查询参数不能重复")
    args = {k: v[0] for k, v in query.items()}
    endpoint = url.path.rsplit("/", 1)[-1]
    if endpoint == "feed":
        if set(args) - {"q", "topic", "view", "order", "offset", "limit"}:
            raise Problem("未知查询参数")
        for key in ("offset", "limit"):
            if key in args:
                if not args[key].isascii() or not args[key].isdecimal():
                    raise Problem("分页参数必须是整数")
                args[key] = int(args[key])
        return hub.feed(actor_id, analysis=analysis, **args)
    if endpoint == "details":
        exact(args, {"exploration_id"})
        return {**hub.details(args["exploration_id"], actor_id=actor_id),
                "following": args["exploration_id"] in hub._marks(actor_id, "follow") if actor_id else False,
                "bookmarked": args["exploration_id"] in hub._marks(actor_id, "bookmark") if actor_id else False}
    if endpoint == "label-appeal":
        exact(args, {"exploration_id"})
        if not actor_id:
            raise Problem("请先创建或恢复身份", 401)
        return hub.author_label_status(actor_id, args["exploration_id"])
    if args:
        raise Problem("这个接口不接受查询参数")
    if endpoint == "topics":
        return hub.topics()
    if endpoint == "notifications":
        return hub.notifications(actor_id, research=research, analysis=analysis)
    if endpoint == "behavior-cases":
        return hub.behavior_cases(actor_id)
    if endpoint == "export":
        return hub.personal_export(actor_id)
    raise Problem("Not found", 404)


def post(hub, path, actor_id, payload, research=None, analysis=None):
    name = path.rsplit("/", 1)[-1]
    if name == "label-appeal":
        if (type(payload) is not dict
                or not set(payload) <= {"label_id", "exploration_id", "body", "reason"}
                or not ("label_id" in payload or "exploration_id" in payload)
                or ("body" in payload) == ("reason" in payload)):
            raise Problem("字段不正确")
        body = payload["body"] if "body" in payload else payload["reason"]
        return hub.appeal_label(
            actor_id=actor_id,
            label_id=payload.get("label_id"),
            body=body,
            exploration_id=payload.get("exploration_id"),
        )
    endpoints = {
        "details": ({"exploration_id", "topics", "help", "expected_version"}, hub.set_details),
        "mark": ({"exploration_id", "kind", "enabled"}, hub.mark),
        "read": ({"record_ids"}, hub.mark_read),
        "import": ({"bundle"}, hub.personal_import),
        "behavior-report": ({"target_id", "category", "reason"}, hub.submit_behavior),
        "behavior-response": ({"case_id", "body"}, hub.respond_behavior),
        "behavior-appeal": ({"case_id", "body"}, hub.appeal_behavior),
    }
    if name not in endpoints:
        raise Problem("Not found", 404)
    fields, action = endpoints[name]
    exact(payload, fields)
    if name in {'read', 'import'}:
        return action(actor_id=actor_id, research=research, analysis=analysis, **payload)
    if name == 'behavior-report':
        return action(actor_id=actor_id, research=research, **payload)
    return action(actor_id=actor_id, **payload)
