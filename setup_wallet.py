"""
初始化本地专用 Polygon 热钱包与 .env 风控配置
严格实现本地私钥隔离：不在日志中打印完整私钥，仅写入本地 .env 文件，并在控制台输出收款地址。
"""
import os
import secrets
from pathlib import Path
from eth_account import Account

ENV_PATH = Path(__file__).parent / ".env"
GITIGNORE_PATH = Path(__file__).parent / ".gitignore"

# 兜底的最小 .gitignore —— 仅在文件缺失时写入。
# 注意：绝不能覆盖已存在的 .gitignore，否则会把扩展后的忽略规则打回原形。
MINIMAL_GITIGNORE = """# ===== 密钥与凭证（绝不入库）=====
.env
.env.*
*.pem
*.key

# ===== 本地运行状态 =====
state/
data/

# ===== 日志 =====
logs/
*.log

# ===== 一次性 burner 钱包目录 =====
003_MataMask 钱包/
*钱包*/
*wallet*.json
metamask-password.txt

# ===== 构建产物 =====
build_launcher/
dist/
*.exe
*.spec

# ===== Python 缓存 =====
__pycache__/
*.py[cod]
"""


def init_wallet():
    # 确保 .gitignore 保护 .env（只在缺失时创建，不覆盖已有规则）
    if not GITIGNORE_PATH.exists():
        GITIGNORE_PATH.write_text(MINIMAL_GITIGNORE, encoding="utf-8")
        print(f"[已创建] {GITIGNORE_PATH.name}（密钥保护规则）")

    if ENV_PATH.exists():
        # 读取已有地址
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
        addr = None
        for line in lines:
            if line.startswith("WALLET_ADDRESS="):
                addr = line.split("=", 1)[1].strip()
        if addr:
            print(f"[已存在钱包] 专属 Polygon 钱包地址: {addr}")
            return addr

    # 生成 32 字节高熵私钥
    priv_key = "0x" + secrets.token_hex(32)
    acct = Account.from_key(priv_key)
    address = acct.address

    env_content = f"""# ==========================================================
# Polymarket 10U 量化系统本地配置文件 (严禁泄露此文件!)
# ==========================================================
WALLET_ADDRESS={address}
PRIVATE_KEY={priv_key}

# Polygon 节点 RPC (支持多节点自动故障切换)
POLYGON_RPC=https://polygon-bor-rpc.publicnode.com

# Polymarket CLOB 配置
CLOB_HOST=https://clob.polymarket.com
CHAIN_ID=137

# ==========================================================
# 三重安全开关 (Triple Safety Switch)
# 默认开启 PAPER=true 模拟实盘，充值到账并授权后改为 PAPER=false, ALLOW_LIVE=true
# ==========================================================
PAPER=true
ALLOW_LIVE=false

# ==========================================================
# 10U 预授权风控信封 (Pre-Authorized Risk Envelope)
# ==========================================================
MAX_CAPITAL_USDC=10.0
MAX_SINGLE_BET_USDC=2.0
MIN_SINGLE_BET_USDC=1.0
MAX_OPEN_POSITIONS=4
DAILY_LOSS_LIMIT_USDC=2.0
"""
    ENV_PATH.write_text(env_content, encoding="utf-8")
    print("==========================================================")
    print("已成功在本地生成全新 Polygon 专用热钱包！")
    print(f"专属充值地址 (Polygon 网络): {address}")
    print(f"私钥已安全保存至本地: {ENV_PATH} (未在屏幕打印，防截屏泄露)")
    print("==========================================================")
    return address


if __name__ == "__main__":
    init_wallet()
