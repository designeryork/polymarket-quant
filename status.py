"""
终端战报与链上双钱包状态查看器 (status.py)
同时监控：
1. EOA 签名钱包 (0x6A83...) 与 Deposit Wallet (0xaF56...) 的 POL / pUSD / USDC.e / 原生USDC 余额
2. Deposit Wallet 链上合约部署状态 (deployed)
3. 模拟盘 / 实盘当前净值、持仓浮盈亏、已平仓收益与风控扫描日志
"""
import os
import json
from pathlib import Path
from dotenv import load_dotenv
from web3 import Web3

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
PORTFOLIO_FILE = BASE_DIR / "state" / "portfolio.json"

PUSD_ADDRESS = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E_ADDRESS = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
USDC_NATIVE_ADDRESS = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
DEPOSIT_WALLET_FACTORY = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

FACTORY_ABI = [
    {
        "inputs": [{"type": "bytes32"}],
        "name": "predictWalletAddress",
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def get_w3():
    rpcs = [
        os.getenv("POLYGON_RPC", "https://1rpc.io/matic"),
        "https://1rpc.io/matic",
        "https://polygon.drpc.org",
        "https://polygon-bor-rpc.publicnode.com",
    ]
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def check_onchain_status(eoa_addr: str):
    w3 = get_w3()
    if not w3:
        return None
    try:
        eoa = Web3.to_checksum_address(eoa_addr)
        factory = w3.eth.contract(address=DEPOSIT_WALLET_FACTORY, abi=FACTORY_ABI)
        salt_b32 = bytes.fromhex(eoa[2:].lower().rjust(64, "0"))
        dep_wallet = Web3.to_checksum_address(factory.functions.predictWalletAddress(salt_b32).call())
        dep_code = w3.eth.get_code(dep_wallet)

        pusd_c = w3.eth.contract(address=PUSD_ADDRESS, abi=ERC20_ABI)
        usdce_c = w3.eth.contract(address=USDC_E_ADDRESS, abi=ERC20_ABI)
        usdc_nat_c = w3.eth.contract(address=USDC_NATIVE_ADDRESS, abi=ERC20_ABI)

        return {
            "eoa": eoa,
            "deposit_wallet": dep_wallet,
            "deposit_wallet_deployed": len(dep_code) > 0,
            "pol_eoa": round(float(w3.from_wei(w3.eth.get_balance(eoa), "ether")), 4),
            "pusd_eoa": round(pusd_c.functions.balanceOf(eoa).call() / 1e6, 2),
            "pusd_deposit": round(pusd_c.functions.balanceOf(dep_wallet).call() / 1e6, 2),
            "usdce_eoa": round(usdce_c.functions.balanceOf(eoa).call() / 1e6, 2),
            "usdc_native_eoa": round(usdc_nat_c.functions.balanceOf(eoa).call() / 1e6, 2),
        }
    except Exception:
        return None


def print_report():
    wallet = os.getenv("WALLET_ADDRESS", "未初始化")
    paper_flag = os.getenv("PAPER", "true").lower() == "true"
    live_flag = os.getenv("ALLOW_LIVE", "false").lower() == "true"
    env_mode_str = "LIVE (实盘模式已开启)" if (not paper_flag and live_flag) else "PAPER (模拟实盘盘口撮合)"

    print("=" * 76)
    print("  Polymarket 10U 量化系统 — 实时盈亏与链上双钱包战报终端")
    print("=" * 76)
    print(f"  EOA 签名钱包     : {wallet}")

    info = check_onchain_status(wallet) if wallet.startswith("0x") else None
    if info:
        dep_status = "已部署激活 (Deployed)" if info["deposit_wallet_deployed"] else "待部署激活 (Not Deployed)"
        print(f"  Deposit Wallet   : {info['deposit_wallet']} [{dep_status}]")
        print(
            f"  链上实盘本金     : EOA 持有 {info['pusd_eoa']:.2f} pUSD | "
            f"Deposit Wallet 持有 {info['pusd_deposit']:.2f} pUSD"
        )
        print(
            f"  其他链上余额     : {info['pol_eoa']:.4f} POL (Gas) | "
            f"{info['usdce_eoa']:.2f} USDC.e | {info['usdc_native_eoa']:.2f} 原生USDC"
        )
    else:
        print("  链上实盘余额     : [RPC 节点连接中]")

    print(f"  当前配置模式     : {env_mode_str}")

    if not PORTFOLIO_FILE.exists():
        print("-" * 76)
        print("  尚未生成策略状态文件，请运行: python quant_engine.py")
        print("=" * 76)
        return

    st = json.loads(PORTFOLIO_FILE.read_text(encoding="utf-8"))
    total_pnl = st["total_equity_usdc"] - st["initial_capital_usdc"]
    reconciliation = st.get("reconciliation") or {}
    print("-" * 76)
    print(f"  账本记录模式     : {st.get('mode', env_mode_str)} | 最后更新: {st['last_updated']}")
    if reconciliation.get("status") == "OK":
        print(
            f"  链上/CLOB 对账   : 已完成 | {reconciliation.get('reconciled_at')} | "
            f"真实成交 {reconciliation.get('trade_count')} 笔 | 未完订单 {reconciliation.get('open_order_count')} 笔"
        )
    else:
        print("  链上/CLOB 对账   : 尚未完成，以下仅为旧本地账本")
    print(
        f"  对账账户总净值   : {st['total_equity_usdc']:.6f} pUSD  "
        f"(初始: {st['initial_capital_usdc']:.6f} | 总收益率: {st['total_return_pct']:+.2f}%)"
    )
    print(
        f"  对账累计总盈亏   : {total_pnl:+.6f} pUSD  "
        f"[真实卖出盈亏: {st['realized_pnl_usdc']:+.6f} | 持仓浮动: {st['unrealized_pnl_usdc']:+.6f}]"
    )
    print(
        f"  Deposit 可用现金 : {st['cash_usdc']:.6f} pUSD | "
        f"EOA 闲置: {st.get('eoa_idle_pusd', 0):.6f} pUSD | 实际持仓: {len(st['open_positions'])} 个"
    )

    # 在途挂单账本 + 单实例锁 + 网络通道（确认没有重复实例、没有悬挂卖单、走的是哪条线路）
    try:
        import order_ledger
        import single_instance
        import proxy_detector

        pending = order_ledger.summary()
        lock = single_instance.lock_status()
        proxy = proxy_detector.detect_proxy()
        exit_info = proxy.get("exit") or {}
        print("-" * 76)
        print(
            f"  在途挂单账本     : 共 {pending.get('total', 0)} 笔 | 阻塞中 {pending.get('blocking', 0)} 笔 | "
            f"在途买单 ${pending.get('open_buy_notional_usdc', 0):.2f} | "
            f"在途卖单 ${pending.get('open_sell_notional_usdc', 0):.2f}"
        )
        if lock.get("held"):
            alive = "存活" if lock.get("alive") else "已退出(陈旧锁)"
            print(
                f"  单实例锁         : 已持有 | PID {lock.get('pid')} | {lock.get('role','')} | "
                f"{lock.get('started_at_utc','')} | {alive}"
            )
        else:
            print("  单实例锁         : 未持有（当前无后台循环在运行）")
        proxy_text = proxy.get("proxy_url") or "直连（未使用代理）"
        print(
            f"  网络通道         : {proxy_text} | 延迟 {proxy.get('latency_s', 0)}s | "
            f"来源 {proxy.get('source','')}"
        )
        if exit_info.get("ip"):
            print(
                f"  出口位置         : {exit_info.get('ip')} | "
                f"{exit_info.get('country','')} {exit_info.get('region','')} {exit_info.get('city','')} | "
                f"{exit_info.get('org','')}"
            )
    except Exception as exc:
        print(f"  在途挂单/锁状态  : [读取失败] {exc}")

    print("=" * 76)


if __name__ == "__main__":
    print_report()
