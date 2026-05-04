"""
generate_api_creds.py — Generate Polymarket API credentials
Jalankan: python generate_api_creds.py
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

PRIVATE_KEY = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
FUNDER      = os.environ.get("POLYMARKET_FUNDER", "").strip()


def main() -> int:
    if not PRIVATE_KEY or PRIVATE_KEY.startswith("0x_PASTE"):
        print("[ERROR] POLYMARKET_PRIVATE_KEY belum diisi di .env")
        return 1
    if not FUNDER or FUNDER.startswith("0x_PASTE"):
        print("[ERROR] POLYMARKET_FUNDER belum diisi di .env")
        return 1
    if not PRIVATE_KEY.startswith("0x") or len(PRIVATE_KEY) != 66:
        print("[ERROR] POLYMARKET_PRIVATE_KEY format salah (harus 0x + 64 hex)")
        return 1
    if not FUNDER.startswith("0x") or len(FUNDER) != 42:
        print("[ERROR] POLYMARKET_FUNDER format salah (harus 0x + 40 hex)")
        return 1

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON
    except ImportError:
        print("[ERROR] Library py-clob-client belum terinstall.")
        print("Jalankan: pip install -r requirements.txt")
        return 1

    print("Connecting ke Polymarket CLOB...")
    try:
        client = ClobClient(
            host        = "https://clob.polymarket.com",
            chain_id    = POLYGON,
            private_key = PRIVATE_KEY,
            funder      = FUNDER,
        )
    except Exception as e:
        print(f"[ERROR] Gagal init client: {e}")
        return 1

    try:
        creds = client.create_or_derive_api_key()
        print("\n✅ API Credentials berhasil digenerate!\n")
        print(f"API_KEY:        {creds.api_key}")
        print(f"API_SECRET:     {creds.api_secret}")
        print(f"API_PASSPHRASE: {creds.api_passphrase}")
        print()
        print("Copy 3 nilai di atas ke file .env kamu:")
        print(f"  POLYMARKET_API_KEY={creds.api_key}")
        print(f"  POLYMARKET_API_SECRET={creds.api_secret}")
        print(f"  POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")
        return 0
    except Exception as e:
        print(f"\n[ERROR] Gagal generate API key: {e}")
        print("Pastikan private key & funder address benar, dan koneksi internet stabil.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
