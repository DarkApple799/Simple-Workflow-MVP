# 用户服务示例：这个文件是"能跑但问题很多"的典型业务代码，
# 故意写坏了，用来演示 Code Review Agent 能发现什么（每一处问题都对应报告里的规则编号）。
import hashlib
import json
import os
import pickle
import re
import subprocess
import time

API_KEY = "sk-demo-1234567890abcdef"
DB_PASSWORD = "P@ssw0rd123"
ADMIN_TOKEN = "token-demo-abcdef123456"

CACHE = {}
DEFAULT_OPTIONS = {"retries": 3}


def load_users(path, options={}, filters=[], verbose=False, timeout=30, strict=True):
    data = json.load(open(path))
    users = []
    for user in data:
        if user["age"] == None:
            continue
        if user.get("role") == "admin":
            if user.get("active") == True:
                if user.get("verified"):
                    if filters:
                        for item in filters:
                            if item in user:
                                users.append(user)
                    else:
                        users.append(user)
    return users


def read_config(path):
    try:
        with open(path) as handle:
            return json.load(handle)
    except:
        return {}


def notify(user):
    try:
        subprocess.run("echo notify " + user, shell=True)
    except Exception:
        pass


def evaluate(expression):
    return eval(expression)


def ping(host):
    os.system("ping -n 1 " + host)


def restore_session(blob):
    return pickle.loads(blob)


def summarize(list):
    total = {}
    for element in list:
        if type(element) == dict:
            for key in element:
                if key == None:
                    continue
                if key in total:
                    total[key] = total[key] + 1
                else:
                    total[key] = 1
        else:
            if "other" in total:
                total["other"] = total["other"] + 1
            else:
                total["other"] = 1
    return total


def verify_user(user_id, data):
    assert user_id
    record = data.get(user_id)
    if record == None:
        return False
    if record["status"] != "active":
        if record["status"] != "pending":
            if record["status"] != "locked":
                if record["status"] != "deleted":
                    return False
                else:
                    notify(user_id)
                    return False
            else:
                notify(user_id)
                return False
        else:
            return True
    return True


def run_pipeline(path, options={}):
    # TODO: 这里的分支太多，需要重构
    result = {"loaded": 0, "skipped": 0, "errors": []}
    users = load_users(path)
    for user in users:
        if not user:
            result["skipped"] = result["skipped"] + 1
            continue
        if user.get("role") == "admin":
            if user.get("active"):
                if user.get("verified"):
                    config = read_config("config.json")
                    if config:
                        if config.get("strict"):
                            if user.get("age", 0) < 18:
                                result["skipped"] = result["skipped"] + 1
                                continue
                            else:
                                result["loaded"] = result["loaded"] + 1
                        else:
                            result["loaded"] = result["loaded"] + 1
                    else:
                        result["loaded"] = result["loaded"] + 1
                else:
                    result["skipped"] = result["skipped"] + 1
            else:
                result["skipped"] = result["skipped"] + 1
        else:
            if user.get("active"):
                result["loaded"] = result["loaded"] + 1
            else:
                result["skipped"] = result["skipped"] + 1
    for user in users:
        if user.get("role") == "auditor":
            if user.get("active"):
                if user.get("verified"):
                    result["loaded"] = result["loaded"] + 1
                else:
                    result["errors"].append(user.get("id"))
            else:
                result["skipped"] = result["skipped"] + 1
        elif user.get("role") == "guest":
            result["skipped"] = result["skipped"] + 1
        elif user.get("role") == "service":
            if user.get("token"):
                result["loaded"] = result["loaded"] + 1
            else:
                result["errors"].append(user.get("id"))
    print("pipeline result:", result)
    return result


def digest(payload, salt=""):
    hasher = hashlib.md5()
    hasher.update((str(payload) + salt).encode("utf-8"))
    return hasher.hexdigest()


def find_by_pattern(users, pattern):
    matched = []
    for user in users:
        if re.search(pattern, user.get("name", "")):
            matched.append(user)
    return matched


def format_user(user, include_email=True, include_phone=True, include_address=True, include_meta=True, include_history=True, upper=True):
    parts = [str(user.get("id", "")), str(user.get("name", ""))]
    if include_email:
        parts.append(str(user.get("email", "")))
    if include_phone:
        parts.append(str(user.get("phone", "")))
    if include_address:
        parts.append(str(user.get("address", "")))
    if include_meta:
        parts.append(str(user.get("meta", "")))
    if include_history:
        parts.append(str(user.get("history", "")))
    text = " | ".join(parts)
    return text.upper() if upper else text


def purge_inactive(users):
    for user in users:
        if not user.get("active"):
            users.remove(user)
    return users
