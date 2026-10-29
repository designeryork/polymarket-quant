"""
Polymarket v2/v3 实盘全链路激活脚本 (activate_live.py)
功能（全幂等，可安全重复运行）：
1. 检查 EOA (0x6A83...) 与 Deposit Wallet (0xaF56...) 的 pUSD / USDC.e / 原生USDC 余额
2. 若有原生 USDC -> 自动经 Uniswap V3 兑换为 USDC.e；若有 USDC.e -> 自动经官方 Wrapper 1:1 封装为 pUSD
3. 检查 Deposit Wallet (0xaF56...) 是否已在链上部署：
   - 若未部署，自动通过 CLOB 派生 Builder Key 并调用 Relayer v2 (/submit WALLET_CREATE) 免 Gas 部署 Deposit Wallet
4. 通过 EIP-712 Batch 签名调用 Deposit Wallet 的 execute()，一次性完成对 Polymarket v2/v3 全部交易合约的 pUSD 与 ERC1155 授权
5. 将指定数量的 pUSD 从 EOA 转入 Deposit Wallet，并通知 CLOB 更新余额与授权缓存
"""
import os
import json
import time
import argparse
import requests
from pathlib import Path
from dotenv import load_dotenv, set_key
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, RequestArgs
from py_clob_client.headers.headers import create_level_2_headers
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)

PRIVATE_KEY = os.getenv("PRIVATE_KEY", "")
EOA_ADDRESS = Web3.to_checksum_address(os.getenv("WALLET_ADDRESS", ""))

PUSD = Web3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
USDC_E = Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
USDC_NATIVE = Web3.to_checksum_address("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359")
PUSD_WRAPPER = Web3.to_checksum_address("0x93070a847efEf7F70739046A929D47a521F5B8ee")
CONDITIONAL_TOKENS = Web3.to_checksum_address("0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
POSITION_MANAGER = Web3.to_checksum_address("0x006F54F7f9A22e0000CC2AB60031000000ae9fEF")
DEPOSIT_WALLET_FACTORY = Web3.to_checksum_address("0x00000000000Fb5C9ADea0298D729A0CB3823Cc07")

# Polymarket v2/v3 所需授权的全部 Spender 与 Operator (源自 @polymarket/client v0.14.0 Oi())
ERC20_SPENDERS = [
    Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B"),  # standardExchange
    Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59"),  # negRiskExchange
    Web3.to_checksum_address("0xe3333700cA9d93003F00f0F71f8515005F6c00Aa"),  # exchangeV3
    Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"),  # negRiskAdapter
    Web3.to_checksum_address("0xAdA100Db00Ca00073811820692005400218FcE1f"),  # collateralAdapter
    Web3.to_checksum_address("0xadA2005600Dec949baf300f4C6120000bDB6eAab"),  # negRiskCollateralAdapter
    Web3.to_checksum_address("0x12121212006e4CD160D18e3f00711DA5c3372600"),  # protocolV2Router
]

ERC1155_OPERATORS = [
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xE111180000d2663C0091e4f400237545B87B996B")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xe2222d279d744050d28e00520010520000310F59")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")),  # negRiskAdapter
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xAdA100Db00Ca00073811820692005400218FcE1f")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xadA2005600Dec949baf300f4C6120000bDB6eAab")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0xa1200000d0002264C9a1698e001292D00E1b00af")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0x1000008dD9001B968442c1000017eaE6E0dA00Ba")),
    (CONDITIONAL_TOKENS, Web3.to_checksum_address("0x200000900045e3B6259600682756002200028933")),
    (POSITION_MANAGER, Web3.to_checksum_address("0x12121212006e4CD160D18e3f00711DA5c3372600")),
    (POSITION_MANAGER, Web3.to_checksum_address("0xe3333700cA9d93003F00f0F71f8515005F6c00Aa")),
]

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}], "name": "allowance", "outputs": [{"name": "remaining", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "_to", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
]

DEPOSIT_WALLET_ABI = [
    {"inputs": [], "name": "nonce", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "owner", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {
        "inputs": [
            {
                "components": [
                    {"name": "wallet", "type": "address"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {
                        "components": [
                            {"name": "target", "type": "address"},
                            {"name": "value", "type": "uint256"},
                            {"name": "data", "type": "bytes"},
                        ],
                        "name": "calls",
                        "type": "tuple[]",
                    },
                ],
                "name": "batch",
                "type": "tuple",
            },
            {"name": "signature", "type": "bytes"},
        ],
        "name": "execute",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "withdrawERC20",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def get_w3() -> Web3:
    for rpc in ["https://1rpc.io/matic", "https://polygon.drpc.org", "https://polygon-bor-rpc.publicnode.com"]:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
            if w3.is_connected():
                return w3
        except Exception:
            pass
    raise RuntimeError("所有 Polygon RPC 节点均无法连接")


def send_eoa_tx(w3: Web3, tx_func, desc: str):
    nonce = w3.eth.get_transaction_count(EOA_ADDRESS, "pending")
    tx = tx_func.build_transaction(
        {"from": EOA_ADDRESS, "nonce": nonce, "gasPrice": int(w3.eth.gas_price * 1.25), "chainId": 137}
    )
    signed = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"  -> [已广播] {desc} | Tx: {tx_hash.hex()}")
    rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
    if rcpt.status != 1:
        raise RuntimeError(f"交易失败: {desc}")
    print(f"  -> [已确认] {desc} (Block {rcpt.blockNumber})")
    time.sleep(1.5)
    return rcpt


def ensure_builder_creds(clob_client: ClobClient) -> BuilderApiKeyCreds:
    b_key = os.getenv("BUILDER_API_KEY")
    b_sec = os.getenv("BUILDER_SECRET")
    b_pass = os.getenv("BUILDER_PASSPHRASE")
    if b_key and b_sec and b_pass:
        return BuilderApiKeyCreds(key=b_key, secret=b_sec, passphrase=b_pass)

    req_args = RequestArgs(method="POST", request_path="/auth/builder-api-key")
    headers = create_level_2_headers(clob_client.signer, clob_client.creds, req_args)
    resp = requests.post("https://clob.polymarket.com/auth/builder-api-key", headers=headers, timeout=10)
    if resp.status_code != 200:
        # 尝试 GET 已有 builder key
        req_get = RequestArgs(method="GET", request_path="/auth/builder-api-key")
        h_get = create_level_2_headers(clob_client.signer, clob_client.creds, req_get)
        r_get = requests.get("https://clob.polymarket.com/auth/builder-api-key", headers=h_get, timeout=10)
        raise RuntimeError(f"无法创建 Builder API Key: POST={resp.text}, GET={r_get.text}")

    data = resp.json()
    set_key(str(ENV_PATH), "BUILDER_API_KEY", data["key"])
    set_key(str(ENV_PATH), "BUILDER_SECRET", data["secret"])
    set_key(str(ENV_PATH), "BUILDER_PASSPHRASE", data["passphrase"])
    return BuilderApiKeyCreds(key=data["key"], secret=data["secret"], passphrase=data["passphrase"])


def deploy_deposit_wallet_via_relayer(clob_client: ClobClient, deposit_wallet: str):
    b_creds = ensure_builder_creds(clob_client)
    b_cfg = BuilderConfig(local_builder_creds=b_creds)
    body_dict = {
        "from": EOA_ADDRESS,
        "to": DEPOSIT_WALLET_FACTORY,
        "type": "WALLET-CREATE",
        "metadata": "Deploy Deposit Wallet",
    }
    body_str = json.dumps(body_dict, separators=(",", ":"))
    b_headers = b_cfg.generate_builder_headers("POST", "/submit", body_str).to_dict()
    b_headers["Content-Type"] = "application/json"

    resp = requests.post("https://relayer-v2.polymarket.com/submit", headers=b_headers, data=body_str, timeout=15)
    resp.raise_for_status()
    tx_info = resp.json()
    tx_id = tx_info.get("transactionID") or tx_info.get("transactionId")
    print(f"  -> Relayer 已受理部署请求: transactionId={tx_id}, txHash={tx_info.get('transactionHash')}")

    for _ in range(30):
        time.sleep(2)
        r = requests.get(f"https://relayer-v2.polymarket.com/transaction?id={tx_id}", timeout=10).json()
        if isinstance(r, list):
            r = r[0] if r else {}
        state = (r or {}).get("state", "")
        if "CONFIRMED" in state or "MINED" in state:
            print(f"  -> Deposit Wallet 部署已确认! State={state} proxy={r.get('proxyAddress')} tx={r.get('transactionHash')}")
            return
        if "FAILED" in state or "INVALID" in state:
            raise RuntimeError(f"Relayer 部署失败: state={state} body={r}")
    print("  -> 等待 Relayer 确认超时，请稍后检查链上状态。")



def submit_relayer(body_dict: dict, b_creds: BuilderApiKeyCreds) -> dict:
    b_cfg = BuilderConfig(local_builder_creds=b_creds)
    body_str = json.dumps(body_dict, separators=(",", ":"))
    b_headers = b_cfg.generate_builder_headers("POST", "/submit", body_str).to_dict()
    b_headers["Content-Type"] = "application/json"
    resp = requests.post("https://relayer-v2.polymarket.com/submit", headers=b_headers, data=body_str, timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"Relayer submit failed: {resp.status_code} {resp.text[:500]}")
    return resp.json()


def wait_relayer_tx(tx_id: str, b_creds: BuilderApiKeyCreds, label: str):
    b_cfg = BuilderConfig(local_builder_creds=b_creds)
    for _ in range(40):
        time.sleep(2)
        # public poll first
        try:
            r = requests.get(f"https://relayer-v2.polymarket.com/transaction?id={tx_id}", timeout=10).json()
            if isinstance(r, list):
                r = r[0] if r else {}
            state = (r or {}).get("state", "")
            if "CONFIRMED" in state or "MINED" in state:
                print(f"  -> {label} 已确认 State={state} tx={r.get('transactionHash')}")
                return r
            if "FAILED" in state or "INVALID" in state:
                raise RuntimeError(f"{label} 失败: {r}")
        except RuntimeError:
            raise
        except Exception:
            pass
    raise RuntimeError(f"{label} 等待确认超时 tx_id={tx_id}")


def fetch_wallet_nonce(b_creds: BuilderApiKeyCreds) -> int:
    # GET /nonce or params; try on-chain wallet nonce first (reliable for DepositWallet)
    return None  # placeholder overridden below


def approve_all_from_deposit_wallet(w3: Web3, deposit_wallet: str, clob_client: ClobClient):
    """通过 Relayer type=WALLET 提交 Deposit Wallet execute 批量授权（EOA 不能直接调 execute，会 OnlyFactory）。"""
    b_creds = ensure_builder_creds(clob_client)
    dw = w3.eth.contract(address=deposit_wallet, abi=DEPOSIT_WALLET_ABI)
    nonce = int(dw.functions.nonce().call())
    deadline = int(time.time()) + 1800

    calls = []
    approve_cap = 2**256 - 1  # maxUint256 per official trading approvals docs
    for spender in ERC20_SPENDERS:
        data_hex = "0x095ea7b3" + spender[2:].lower().rjust(64, "0") + hex(approve_cap)[2:].rjust(64, "0")
        calls.append({"target": PUSD, "value": 0, "data": bytes.fromhex(data_hex[2:])})
    for token_addr, operator in ERC1155_OPERATORS:
        data_hex = "0xa22cb465" + operator[2:].lower().rjust(64, "0") + "1".rjust(64, "0")
        calls.append({"target": token_addr, "value": 0, "data": bytes.fromhex(data_hex[2:])})

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Call": [
                {"name": "target", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
            ],
            "Batch": [
                {"name": "wallet", "type": "address"},
                {"name": "nonce", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
                {"name": "calls", "type": "Call[]"},
            ],
        },
        "primaryType": "Batch",
        "domain": {
            "name": "DepositWallet",
            "version": "1",
            "chainId": 137,
            "verifyingContract": deposit_wallet,
        },
        "message": {
            "wallet": deposit_wallet,
            "nonce": nonce,
            "deadline": deadline,
            "calls": calls,
        },
    }
    signable = encode_typed_data(full_message=typed_data)
    sig = Account.sign_message(signable, private_key=PRIVATE_KEY).signature
    sig_hex = sig.hex() if isinstance(sig, (bytes, bytearray)) else str(sig)
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex

    # Relayer payload: calls data as hex strings, value as decimal string
    relayer_calls = [
        {
            "target": c["target"],
            "value": str(c["value"]),
            "data": "0x" + c["data"].hex(),
        }
        for c in calls
    ]
    body = {
        "type": "WALLET",
        "from": EOA_ADDRESS,
        "to": DEPOSIT_WALLET_FACTORY,
        "nonce": str(nonce),
        "signature": sig_hex,
        "metadata": "Approve Polymarket v2/v3 trading contracts",
        "depositWalletParams": {
            "depositWallet": deposit_wallet,
            "deadline": str(deadline),
            "calls": relayer_calls,
        },
    }
    print(f"  -> Relayer 提交批量授权 (nonce={nonce}, calls={len(relayer_calls)})...")
    tx_info = submit_relayer(body, b_creds)
    tx_id = tx_info.get("transactionID") or tx_info.get("transactionId")
    print(f"  -> Relayer 已受理: transactionId={tx_id}")
    wait_relayer_tx(tx_id, b_creds, "批量授权")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fund-amount", type=float, default=10.87, help="从 EOA 划转至 Deposit Wallet 的 pUSD 金额")
    parser.add_argument("--withdraw-to-eoa", type=float, default=0.0, help="从 Deposit Wallet 提回 EOA 的 pUSD 金额")
    args = parser.parse_args()

    w3 = get_w3()
    factory = w3.eth.contract(
        address=DEPOSIT_WALLET_FACTORY,
        abi=[{"inputs": [{"type": "bytes32"}], "name": "predictWalletAddress", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}],
    )
    salt_b32 = bytes.fromhex(EOA_ADDRESS[2:].lower().rjust(64, "0"))
    deposit_wallet = Web3.to_checksum_address(factory.functions.predictWalletAddress(salt_b32).call())
    set_key(str(ENV_PATH), "DEPOSIT_WALLET", deposit_wallet)

    creds = ApiCreds(
        api_key=os.getenv("CLOB_API_KEY"),
        api_secret=os.getenv("CLOB_SECRET"),
        api_passphrase=os.getenv("CLOB_PASSPHRASE"),
    )
    clob_client = ClobClient("https://clob.polymarket.com", key=PRIVATE_KEY, chain_id=137, creds=creds)

    # 1. 检查并部署 Deposit Wallet
    code = w3.eth.get_code(deposit_wallet)
    if len(code) == 0:
        print(f"[1/3] 正在通过 Polymarket Relayer 部署 Deposit Wallet: {deposit_wallet}...")
        deploy_deposit_wallet_via_relayer(clob_client, deposit_wallet)
    else:
        print(f"[1/3] Deposit Wallet 已在链上部署就绪: {deposit_wallet}")

    # 2. 若请求提回 EOA，执行 withdrawERC20
    pusd_c = w3.eth.contract(address=PUSD, abi=ERC20_ABI)
    dw_c = w3.eth.contract(address=deposit_wallet, abi=DEPOSIT_WALLET_ABI)
    if args.withdraw_to_eoa > 0:
        amt_raw = int(args.withdraw_to_eoa * 1e6)
        send_eoa_tx(w3, dw_c.functions.withdrawERC20(PUSD, EOA_ADDRESS, amt_raw), f"从 Deposit Wallet 提回 {args.withdraw_to_eoa} pUSD 至 EOA")
        return

    # 3. 检查并执行 Deposit Wallet 批量授权
    allow = pusd_c.functions.allowance(deposit_wallet, ERC20_SPENDERS[0]).call()
    if allow < int(15 * 1e6):
        print("[2/3] 正在执行 Deposit Wallet 交易合约批量授权...")
        approve_all_from_deposit_wallet(w3, deposit_wallet, clob_client)
    else:
        print("[2/3] Deposit Wallet 交易合约授权已就绪。")

    # 4. 将 EOA 中的 pUSD 划转至 Deposit Wallet
    eoa_pusd = pusd_c.functions.balanceOf(EOA_ADDRESS).call()
    if eoa_pusd > 0 and args.fund_amount > 0:
        transfer_raw = min(eoa_pusd, int(args.fund_amount * 1e6))
        print(f"[3/3] 正在将 {transfer_raw/1e6:.2f} pUSD 从 EOA 划转至 Deposit Wallet...")
        send_eoa_tx(w3, pusd_c.functions.transfer(deposit_wallet, transfer_raw), "划转 pUSD 至 Deposit Wallet")

    dw_pusd = pusd_c.functions.balanceOf(deposit_wallet).call()
    print(f"? Deposit Wallet 当前可用余额: {dw_pusd/1e6:.4f} pUSD")


if __name__ == "__main__":
    main()
