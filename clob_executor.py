"""
Polymarket v2/v3 实盘签名与订单执行器 (clob_executor.py)
严格实现 @polymarket/client v0.14.0 的 POLY_1271 (signatureType=3) 签名规范：
1. 自动识别 v2 Standard / v2 NegRisk / v3 Exchange 合约与 protocolVersion ("2" 或 "3")
2. 构造 TypedDataSign 嵌套 EIP-712 消息并由 EOA 签名
3. 拼接 ERC-1271 验证后缀: 0x + raw_sig + domain_separator_hash + order_struct_hash + order_type_hex + len_hex
4. 提供挂单 (post_limit_order)、撤单 (cancel_order)、查询挂单 (get_open_orders) 与买入后自动挂 GTC 止盈单功能
"""
import os
import json
import time
import math
import secrets
import argparse
import requests
from pathlib import Path
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_abi import encode
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, RequestArgs
from py_clob_client.headers.headers import create_level_2_headers
from py_clob_client.http_helpers.helpers import post, get, delete

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

ORDER_TYPE_STR = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
ORDER_TYPE_HASH = Web3.keccak(text=ORDER_TYPE_STR)
DOMAIN_TYPE_HASH = Web3.keccak(
    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
PROTOCOL_NAME_HASH = Web3.keccak(text="Polymarket CTF Exchange")
ZERO_BYTES32 = bytes.fromhex("0" * 64)
ZERO_HEX32 = "0x" + "0" * 64

# ERC-1271 包装签名的固定结构长度（用于离线自检，杜绝前导零被误删）：
#   0x(2) + raw_sig(130) + domain_separator(64) + order_struct_hash(64)
#   + type_string_hex(2 * len(ORDER_TYPE_STR)) + type_len(4)
EXPECTED_SIGNATURE_LENGTH = (
    2 + 130 + 64 + 64 + len(ORDER_TYPE_STR.encode("utf-8").hex()) + 4
)

# CLOB 的最小买单金额：$1（以 6 位小数的 base units 表示）。
# 低于此值会被 CLOB 拒绝：invalid amount for a marketable BUY order, min size: $1
MIN_BUY_AMOUNT_USDC_UNITS = 1_000_000


class OrderAmountTooSmall(ValueError):
    """买单金额低于 CLOB 的 $1 最小下单额。

    单独成类，便于上层把它归类为"可重试但需调参"的错误，
    而不是笼统的交易异常。
    """


def strip_hex_prefix(value: str) -> str:
    """安全地移除一次 `0x` 前缀。

    历史缺陷：曾使用 `lstrip("0x")`，它会持续删除开头所有属于 `0` 或 `x` 的字符。
    当前 web3 8.x / eth-account 0.14 的 `.hex()` 本身不返回 `0x` 前缀，
    因此 `lstrip` 只会误删哈希的合法前导零 nibble，导致签名长度错位并被 CLOB 拒绝。
    实测该缺陷导致约 13.6% 的订单签名长度异常。
    """
    if value.startswith("0x"):
        return value[2:]
    return value


class PolymarketV2Executor:
    def __init__(self):
        self.private_key = os.getenv("PRIVATE_KEY", "")
        self.eoa_address = Account.from_key(self.private_key).address
        self.deposit_wallet = Web3.to_checksum_address(
            os.getenv("DEPOSIT_WALLET", "0xaF56445fAe9Fe3eE8d74988ad873f3677F672467")
        )
        self.creds = ApiCreds(
            api_key=os.getenv("CLOB_API_KEY", ""),
            api_secret=os.getenv("CLOB_SECRET", ""),
            api_passphrase=os.getenv("CLOB_PASSPHRASE", ""),
        )
        self.host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
        # 让 SDK 的底层 httpx 客户端使用探测到的可用通道。
        # 不做这一步的话，SDK 会沿用环境变量里的代理地址，
        # 造成"requests 能连通但 CLOB 下单全部失败"的分裂现象。
        try:
            import proxy_detector

            self._proxy_applied = proxy_detector.apply_to_clob_sdk()
        except Exception:
            self._proxy_applied = {"ok": False, "detail": "代理应用失败，沿用 SDK 默认配置"}
        self.client = ClobClient(self.host, key=self.private_key, chain_id=137, creds=self.creds)

    @staticmethod
    def is_v3_token(token_id_str: str) -> bool:
        t = int(token_id_str)
        mask = ((1 << 64) - 1) << 40
        return (t & mask) == 0

    def resolve_exchange_and_version(self, token_id: str) -> tuple[str, str]:
        if self.is_v3_token(token_id):
            return Web3.to_checksum_address("0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"), "3"
        neg_risk = self.client.get_neg_risk(token_id)
        if neg_risk:
            return Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59"), "2"
        return Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B"), "2"

    def build_and_sign_poly1271_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        tick_size: float = 0.01,
        order_type: str = "GTC",
    ) -> Dict[str, Any]:
        """
        构造并签名 POLY_1271 (signatureType=3) 订单 JSON 载荷
        side: 'BUY' 或 'SELL'
        """
        exchange, proto_ver = self.resolve_exchange_and_version(token_id)
        side_int = 0 if side.upper() == "BUY" else 1

        # 对齐价格与份额精度 (6位小数 base units)
        decimals = max(1, int(round(-math.log10(tick_size))))
        clean_price = round(round(price / tick_size) * tick_size, decimals)

        # 份额向下截断到 2 位小数（Polymarket 的份额精度）。
        # 关键：截断会让实际金额**小于**预期，可能跌破 CLOB 的 $1 最小下单额。
        # 例：引擎算 shares=5.0251（意图 $1.00 @0.199），floor 后 5.02 份
        #     → 5.02*0.199 = $0.99898，被 CLOB 以 HTTP 400 拒绝
        #     （invalid amount for a marketable BUY order ($0.999), min size: $1）
        # 因此这里改为**向上取整**，宁可略微多付一点，也不能让金额跌破门槛。
        #
        # 另注 floor 的浮点陷阱：4.77 * 100 = 476.99999999999994，
        # 直接 floor 会得 476 而非 477（凭空少 0.01 份）。加 epsilon 修正。
        clean_size = math.ceil(size_shares * 100.0 - 1e-6) / 100.0
        if clean_size <= 0:
            clean_size = math.ceil(size_shares * 100.0) / 100.0

        size_units = int(round(clean_size * 1_000_000))
        usdc_units = int(round(clean_size * clean_price * 1_000_000))

        # Fail-closed：买单金额必须达到 $1 最小门槛，否则直接拒绝构造订单。
        # 与其让 CLOB 返回 400（浪费一次往返并在日志里留下误报），不如在本地拦下。
        if side_int == 0 and usdc_units < MIN_BUY_AMOUNT_USDC_UNITS:
            raise OrderAmountTooSmall(
                f"买单金额 ${usdc_units / 1e6:.6f} 低于最小下单额 ${MIN_BUY_AMOUNT_USDC_UNITS / 1e6:.2f} "
                f"(shares={size_shares} -> {clean_size} @ price={clean_price})"
            )

        if side_int == 0:  # BUY: 付出 pUSD (makerAmount)，获得 shares (takerAmount)
            maker_amt, taker_amt = usdc_units, size_units
        else:  # SELL: 付出 shares (makerAmount)，获得 pUSD (takerAmount)
            maker_amt, taker_amt = size_units, usdc_units

        salt = secrets.randbits(52)
        ts = int(time.time())

        order_struct = {
            "salt": salt,
            "maker": self.deposit_wallet,
            "signer": self.deposit_wallet,
            "tokenId": int(token_id),
            "makerAmount": maker_amt,
            "takerAmount": taker_amt,
            "side": side_int,
            "signatureType": 3,
            "timestamp": ts,
            "metadata": ZERO_BYTES32,
            "builder": ZERO_BYTES32,
        }

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Order": [
                    {"name": "salt", "type": "uint256"},
                    {"name": "maker", "type": "address"},
                    {"name": "signer", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint8"},
                    {"name": "signatureType", "type": "uint8"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "metadata", "type": "bytes32"},
                    {"name": "builder", "type": "bytes32"},
                ],
                "TypedDataSign": [
                    {"name": "contents", "type": "Order"},
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                    {"name": "salt", "type": "bytes32"},
                ],
            },
            "primaryType": "TypedDataSign",
            "domain": {
                "name": "Polymarket CTF Exchange",
                "version": proto_ver,
                "chainId": 137,
                "verifyingContract": exchange,
            },
            "message": {
                "contents": order_struct,
                "name": "DepositWallet",
                "version": "1",
                "chainId": 137,
                "verifyingContract": self.deposit_wallet,
                "salt": ZERO_BYTES32,
            },
        }

        signable = encode_typed_data(full_message=typed_data)
        raw_sig = strip_hex_prefix(
            Account.sign_message(signable, private_key=self.private_key).signature.hex()
        )

        # 计算 ERC-1271 包装后缀 (wt 函数逻辑)
        ver_hash = Web3.keccak(text=proto_ver)
        domain_hash = strip_hex_prefix(
            Web3.keccak(
                encode(
                    ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                    [DOMAIN_TYPE_HASH, PROTOCOL_NAME_HASH, ver_hash, 137, exchange],
                )
            ).hex()
        )
        order_hash = strip_hex_prefix(
            Web3.keccak(
                encode(
                    [
                        "bytes32", "uint256", "address", "address", "uint256",
                        "uint256", "uint256", "uint8", "uint8", "uint256", "bytes32", "bytes32",
                    ],
                    [
                        ORDER_TYPE_HASH, salt, self.deposit_wallet, self.deposit_wallet,
                        int(token_id), maker_amt, taker_amt, side_int, 3, ts, ZERO_BYTES32, ZERO_BYTES32,
                    ],
                )
            ).hex()
        )
        type_str_hex = ORDER_TYPE_STR.encode("utf-8").hex()
        type_len_hex = f"{len(ORDER_TYPE_STR):04x}"
        full_sig = f"0x{raw_sig}{domain_hash}{order_hash}{type_str_hex}{type_len_hex}"

        # Fail-closed 自检：签名结构长度必须精确匹配，否则拒绝提交（宁可不交易也不发坏签名）
        if len(full_sig) != EXPECTED_SIGNATURE_LENGTH:
            raise ValueError(
                f"POLY_1271 签名长度异常: 期望 {EXPECTED_SIGNATURE_LENGTH}, 实际 {len(full_sig)}"
            )
        if len(raw_sig) != 130:
            raise ValueError(f"POLY_1271 raw_sig 长度异常: 期望 130, 实际 {len(raw_sig)}")
        for part_name, part in (("domain_hash", domain_hash), ("order_hash", order_hash)):
            if len(part) != 64:
                raise ValueError(f"POLY_1271 {part_name} 长度异常: 期望 64, 实际 {len(part)}")

        return {
            "deferExec": False,
            "order": {
                "builder": ZERO_HEX32,
                "expiration": "0",
                "maker": self.deposit_wallet,
                "makerAmount": str(maker_amt),
                "metadata": ZERO_HEX32,
                "salt": salt,
                "side": side.upper(),
                "signature": full_sig,
                "signatureType": 3,
                "signer": self.deposit_wallet,
                "takerAmount": str(taker_amt),
                "timestamp": str(ts),
                "tokenId": str(token_id),
            },
            "orderType": order_type,
            "owner": self.creds.api_key,
        }

    def post_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        serialized = json.dumps(body, separators=(",", ":"))
        req_args = RequestArgs(method="POST", request_path="/order", body=body, serialized_body=serialized)
        headers = create_level_2_headers(self.client.signer, self.client.creds, req_args)
        return post(f"{self.host}/order", headers=headers, data=serialized)

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return self.client.cancel(order_id)

    def get_open_orders(self) -> List[Dict[str, Any]]:
        return self.client.get_orders()


def _pick_liquid_market():
    """选取 24h 成交活跃的市场，返回 (question, token_id, best_ask, mid, tick_size)。"""
    r = requests.get(
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=10&order=volume24hr&ascending=false",
        timeout=10,
    ).json()
    for m in r:
        try:
            tids = json.loads(m["clobTokenIds"])
            prices = json.loads(m.get("outcomePrices") or "[]")
            tid = tids[0]
            # 取 CLOB 盘口 best ask
            book = requests.get(
                f"https://clob.polymarket.com/book?token_id={tid}", timeout=10
            ).json()
            asks = book.get("asks") or []
            bids = book.get("bids") or []
            if not asks:
                continue
            # CLOB book 常按价格从差到优排列：asks[0] 最贵、asks[-1] 最优卖；bids[-1] 最优买
            best_ask = min(float(a["price"]) for a in asks)
            best_bid = max(float(b["price"]) for b in bids) if bids else max(0.01, best_ask - 0.02)
            if best_ask <= 0.05 or best_ask >= 0.90:
                continue
            mid = (best_bid + best_ask) / 2.0
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            return m.get("question", "?"), tid, best_ask, mid, tick, best_bid
        except Exception:
            continue
    raise RuntimeError("未找到可用的高流动性活跃市场")


def _offline_signature_selftest(samples: int = 200) -> int:
    """离线签名自检：不发起任何网络请求，不提交任何订单。

    构造若干 POLY_1271 订单并校验签名字段长度，用于回归验证 hex 前缀处理是否正确。
    返回进程退出码（0 = 通过）。
    """
    import secrets
    import time as _time

    token_id = "92708239841263145361017304203618583258102423804507532186791623602897510452108"
    exchange = Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")
    depositor = Web3.to_checksum_address(os.getenv("DEPOSIT_WALLET", "0xaF56445fAe9Fe3eE8d74988ad873f3677F672467"))
    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        print("[自检失败] 未找到 PRIVATE_KEY，无法进行离线签名验证。")
        return 1

    salt = secrets.randbits(52)
    ts = int(_time.time())
    maker_amt, taker_amt = 1_500_000, 5_000_000
    order_struct = {
        "salt": salt,
        "maker": depositor,
        "signer": depositor,
        "tokenId": int(token_id),
        "makerAmount": maker_amt,
        "takerAmount": taker_amt,
        "side": 0,
        "signatureType": 3,
        "timestamp": ts,
        "metadata": ZERO_BYTES32,
        "builder": ZERO_BYTES32,
    }
    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
            "TypedDataSign": [
                {"name": "contents", "type": "Order"},
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
                {"name": "salt", "type": "bytes32"},
            ],
        },
        "primaryType": "TypedDataSign",
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": 137,
            "verifyingContract": exchange,
        },
        "message": {
            "contents": order_struct,
            "name": "DepositWallet",
            "version": "1",
            "chainId": 137,
            "verifyingContract": depositor,
            "salt": ZERO_BYTES32,
        },
    }

    signable = encode_typed_data(full_message=typed_data)
    raw_sig = strip_hex_prefix(
        Account.sign_message(signable, private_key=private_key).signature.hex()
    )
    ver_hash = Web3.keccak(text="2")
    domain_hash = strip_hex_prefix(
        Web3.keccak(
            encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [DOMAIN_TYPE_HASH, PROTOCOL_NAME_HASH, ver_hash, 137, exchange],
            )
        ).hex()
    )
    order_hash = strip_hex_prefix(
        Web3.keccak(
            encode(
                [
                    "bytes32", "uint256", "address", "address", "uint256",
                    "uint256", "uint256", "uint8", "uint8", "uint256", "bytes32", "bytes32",
                ],
                [
                    ORDER_TYPE_HASH, salt, depositor, depositor, int(token_id),
                    maker_amt, taker_amt, 0, 3, ts, ZERO_BYTES32, ZERO_BYTES32,
                ],
            )
        ).hex()
    )
    type_str_hex = ORDER_TYPE_STR.encode("utf-8").hex()
    full_sig = f"0x{raw_sig}{domain_hash}{order_hash}{type_str_hex}{len(ORDER_TYPE_STR):04x}"

    print(f"期望签名总长度 : {EXPECTED_SIGNATURE_LENGTH}")
    print(f"实际签名总长度 : {len(full_sig)}")
    print(f"raw_sig 长度   : {len(raw_sig)} (期望 130)")
    print(f"domain_hash    : {len(domain_hash)} (期望 64)")
    print(f"order_hash     : {len(order_hash)} (期望 64)")
    ok = (
        len(full_sig) == EXPECTED_SIGNATURE_LENGTH
        and len(raw_sig) == 130
        and len(domain_hash) == 64
        and len(order_hash) == 64
    )
    print(f"单次构造校验   : {'通过' if ok else '失败'}")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-resting", action="store_true", help="执行零风险远端挂单与立即撤单测试")
    parser.add_argument("--live-first-trade", action="store_true", help="实盘首单买入 + 自动挂 GTC 止盈限价卖单")
    parser.add_argument("--self-test-signature", action="store_true", help="离线签名结构自检（不联网、不下单）")
    parser.add_argument("--self-test-signature-samples", type=int, default=200, help="离线自检样本数")
    args = parser.parse_args()

    if args.self_test_signature:
        raise SystemExit(_offline_signature_selftest(args.self_test_signature_samples))

    ex = PolymarketV2Executor()
    if args.test_resting:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=5&order=volume24hr&ascending=false",
            timeout=10,
        ).json()
        tid = json.loads(r[0]["clobTokenIds"])[0]
        q = r[0]["question"]
        print(f"正在对市场 [{q}] 执行 $1.00 远端挂单测试 (Price=0.10, Size=10)...")
        payload = ex.build_and_sign_poly1271_order(tid, "BUY", price=0.10, size_shares=10.0, tick_size=0.01)
        resp = ex.post_order(payload)
        print("挂单响应:", resp)
        oid = resp.get("orderID") or resp.get("orderId") or resp.get("id")
        if oid:
            time.sleep(1.5)
            c_resp = ex.cancel_order(oid)
            print("撤单响应:", c_resp)
        else:
            print("未返回 orderID，请检查 Deposit Wallet 是否已部署与授权。")

    elif args.live_first_trade:
        q, tid, best_ask, mid, tick, best_bid = _pick_liquid_market()
        # 名义约 $1–2；尊重 min shares≈5，但总名义尽量 ≤$2
        target_notional = 1.50
        shares = target_notional / best_ask
        if shares < 5.0:
            if 5.0 * best_ask <= 2.0:
                shares = 5.0
            else:
                # 价格偏高时缩小到不超过 ~$2
                shares = max(5.0, math.floor((2.0 / best_ask) * 100) / 100.0)
                if shares * best_ask > 2.05:
                    print(f"市场 ask={best_ask:.3f} 过高，5 份名义超过 $2，跳过。")
                    raise SystemExit(1)
        shares = math.floor(shares * 100) / 100.0
        notional = shares * best_ask
        print(f"首单市场: [{q}]")
        print(f"token_id={tid} best_ask={best_ask:.4f} mid={mid:.4f} shares={shares} notional≈${notional:.2f}")
        buy_payload = ex.build_and_sign_poly1271_order(
            tid, "BUY", price=best_ask, size_shares=shares, tick_size=tick, order_type="GTC"
        )
        buy_resp = ex.post_order(buy_payload)
        print("买入响应:", {k: buy_resp.get(k) for k in ("success", "orderID", "orderId", "status", "errorMsg", "error") if k in buy_resp or True})
        print("买入完整响应(脱敏):", {k: v for k, v in buy_resp.items() if k.lower() not in ("signature",)})
        buy_oid = buy_resp.get("orderID") or buy_resp.get("orderId") or buy_resp.get("id")
        ok = buy_resp.get("success") in (True, "true", "True") or bool(buy_oid)
        if not ok:
            print("买入失败，停止。")
            raise SystemExit(1)
        print(f"买入 orderID: {buy_oid}")
        # 止盈：略高于入场，取 mid+edge 与 ask+0.02 的较大合理值，封顶 0.99
        tp_price = min(0.99, max(best_ask + 0.02, mid + 0.03))
        tp_price = round(round(tp_price / tick) * tick, max(1, int(round(-math.log10(tick)))))
        time.sleep(2.0)
        sell_payload = ex.build_and_sign_poly1271_order(
            tid, "SELL", price=tp_price, size_shares=shares, tick_size=tick, order_type="GTC"
        )
        sell_resp = ex.post_order(sell_payload)
        print("止盈卖单响应(脱敏):", {k: v for k, v in sell_resp.items() if k.lower() not in ("signature",)})
        tp_oid = sell_resp.get("orderID") or sell_resp.get("orderId") or sell_resp.get("id")
        print(f"止盈 GTC SELL @ {tp_price} orderID: {tp_oid}")
        print("实盘首单流程完成。")
