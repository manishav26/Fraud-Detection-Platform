"""ST4, Kafka permissions: each Redpanda user may do only what docs/kafka.md allows.

    make k8s-kafka-check      (cluster; needs your age key: runs through scripts/k8s_secrets.sh)

Runs rpk inside the Redpanda pod as each user: what it may not do must be refused, what it may
do must work. Writes two marked test messages: to dlq.txn (the topic we inspect by hand), and
to labels as label-sim (ST13; nothing reads labels on the cluster yet).
Allowed writes elsewhere aren't tried: a junk message on model.events would replace the live
promotion (compaction keeps the latest per key), and one on txn.raw would reach the feature job;
those are shown working by a real promotion and the Spark runs. Reads use no consumer group, so
alert-svc's real group position doesn't move.

A write counts as done if the topic gained a message, so run this while no payments are being
sent (ingest writes txn.raw and txn.scored all the time during a replay).

Passwords: from the environment (REDPANDA_*_PASSWORD, loaded by k8s_secrets.sh), into the pod on
stdin. Prints a Markdown table for docs/results/security.md; exit code 1 if anything differs.
"""

import os
import subprocess
import sys

NS = "fraud"
POD = ["kubectl", "-n", NS, "exec", "-i", "redpanda-0", "-c", "redpanda", "--"]
# Inside the pod: the first stdin line is the password (empty = no login), the rest goes to rpk
RUN = ('IFS= read -r p; if [ -n "$p" ]; then export RPK_USER="$1" RPK_PASS="$p" '
       'RPK_SASL_MECHANISM=SCRAM-SHA-256; fi; shift; exec timeout 20 rpk "$@"')
REFUSED = ("AUTHORIZATION_FAILED", "AUTHENTICATION_FAILED", "SASL", "not authorized")
MARK = "kafka-acl-check: a test message, safe to ignore"

PASSWORD = {"ingest": "REDPANDA_INGEST_PASSWORD", "scoring": "REDPANDA_SCORING_PASSWORD",
            "alert-svc": "REDPANDA_ALERTS_PASSWORD", "feature-job": "REDPANDA_FEATURES_PASSWORD",
            "model-admin": "REDPANDA_MODEL_ADMIN_PASSWORD", "tools": "REDPANDA_TOOLS_PASSWORD",
            "label-sim": "REDPANDA_LABEL_SIM_PASSWORD"}


def write(topic):
    return ["topic", "produce", topic], MARK + "\n"


def read(topic):
    return ["topic", "consume", topic, "-n", "1", "-o", "start"], ""


# (who, what, rpk call, allowed). who=None: no login; "wrong": ingest with a wrong password.
CHECKS = [
    (None, "list topics", (["topic", "list"], ""), False),
    ("wrong", "list topics (wrong password)", (["topic", "list"], ""), False),
    ("ingest", "write dlq.txn", write("dlq.txn"), True),
    ("ingest", "write model.events", write("model.events"), False),
    ("ingest", "read txn.scored", read("txn.scored"), False),
    ("scoring", "read model.events", read("model.events"), True),
    ("scoring", "write model.events", write("model.events"), False),
    ("scoring", "read txn.raw", read("txn.raw"), False),
    ("alert-svc", "read txn.scored", read("txn.scored"), True),
    ("alert-svc", "read txn.raw", read("txn.raw"), False),
    ("alert-svc", "write txn.scored", write("txn.scored"), False),
    ("feature-job", "read txn.raw", read("txn.raw"), True),
    ("feature-job", "write txn.raw", write("txn.raw"), False),
    ("feature-job", "read txn.scored", read("txn.scored"), False),
    ("model-admin", "write txn.raw", write("txn.raw"), False),
    ("model-admin", "read txn.scored", read("txn.scored"), False),
    ("tools", "write txn.raw", write("txn.raw"), False),
    ("tools", "write model.events", write("model.events"), False),
    ("tools", "read txn.scored", read("txn.scored"), False),
    # ST13 (threat 10): only the label service identity may write fraud answers
    (None, "write labels", write("labels"), False),
    *((user, "write labels", write("labels"), False)
      for user in ("ingest", "scoring", "alert-svc", "feature-job", "model-admin", "tools")),
    ("label-sim", "write labels", write("labels"), True),
    ("label-sim", "write txn.raw", write("txn.raw"), False),
    ("label-sim", "write model.events", write("model.events"), False),
]


def login(who):
    if who is None:
        return "", ""
    if who == "wrong":
        return "ingest", "not-the-password"
    value = os.environ.get(PASSWORD[who])
    if not value:
        sys.exit(f"{PASSWORD[who]} is not set: run with `make k8s-kafka-check`")
    return who, value


def messages(topic):
    """How many messages the topic holds now (the sum of its partitions' end offsets), as admin."""
    out = subprocess.run(["./scripts/rpk.sh", "topic", "describe", topic, "-p"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True).stdout
    return sum(int(line.split()[-1]) for line in out.splitlines()[1:] if line.strip()[:1].isdigit())


def attempt(who, args, data):
    """'worked', 'refused' or 'unclear: ...'. A write is judged by what landed: the topic's
    message count before and after, because rpk's producer may keep retrying a refused login in
    silence until the timeout (exit 124) instead of printing the refusal."""
    user, password = login(who)
    is_write = args[:2] == ["topic", "produce"]
    before = messages(args[2]) if is_write else None
    r = subprocess.run([*POD, "sh", "-c", RUN, "sh", user or "-", *args],
                       input=f"{password}\n{data}", capture_output=True, text=True, timeout=60,
                       check=False)
    out = r.stdout + r.stderr
    if is_write:
        added = messages(args[2]) - before
        if added > 0:
            return "worked"
        if r.returncode == 124 or any(word in out for word in REFUSED):
            return "refused"
    elif r.returncode == 0:
        return "worked"
    elif any(word in out for word in REFUSED):
        return "refused"
    return f"unclear: exit {r.returncode}, {out.strip().splitlines()[-1:] or ['no output']}"


def main():
    sasl = subprocess.run([*POD, "rpk", "cluster", "config", "get", "enable_sasl"],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          check=False).stdout.strip()
    if sasl != "true":
        sys.exit(f"Redpanda logins are off (enable_sasl = {sasl or '?'}): run make k8s-up")
    wrong = []
    print("| Who | Tries to | Expected | Got |")
    print("|---|---|---|---|")
    for who, what, (args, data), allowed in CHECKS:
        got = attempt(who, args, data)
        expected = "worked" if allowed else "refused"
        ok = got == expected
        print(f"| {who or 'no login'} | {what} | {expected} | {got}{'' if ok else ' WRONG'} |")
        if not ok:
            wrong.append(f"{who or 'no login'} {what}: expected {expected}, got {got}")
    if wrong:
        sys.exit("KAFKA PERMISSIONS FAILED:\n  " + "\n  ".join(wrong))
    print(f"\nKAFKA PERMISSIONS PASSED: all {len(CHECKS)} checks as expected")


if __name__ == "__main__":
    main()
