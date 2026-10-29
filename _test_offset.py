"""Verify Gamma offset parameter actually rotates the result set."""
import proxy_detector

s = proxy_detector.build_session()
s.headers.update({"User-Agent": "t/1.0"})


def get(limit, offset=None):
    p = {"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"}
    if offset:
        p["offset"] = offset
    return s.get("https://gamma-api.polymarket.com/markets", params=p, timeout=20).json()


a = get(30)
b = get(30, 20)
c = get(30, 50)
qa = [m.get("question") for m in a]
qb = [m.get("question") for m in b]
qc = [m.get("question") for m in c]

print(f"offset=0  返回 {len(a):3d} 个 | 首位: {qa[0][:52]}")
print(f"offset=20 返回 {len(b):3d} 个 | 首位: {qb[0][:52] if qb else '-'}")
print(f"offset=50 返回 {len(c):3d} 个 | 首位: {qc[0][:52] if qc else '-'}")
print()
jab = len(set(qa) & set(qb)) / max(1, len(set(qa) | set(qb)))
jac = len(set(qb) & set(qc)) / max(1, len(set(qb) | set(qc)))
print(f"offset 生效?          {qa[0] != qb[0]}")
print(f"offset=0 vs 20 重合度  {jab:.3f}")
print(f"offset=20 vs 50 重合度 {jac:.3f}")
print()
d = get(100)
print(f"limit=100 返回 {len(d)} 个")
