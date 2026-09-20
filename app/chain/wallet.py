"""Ajanin operasyon cuzdani ve otonom Solana transfer katmani.

Gizli anahtar yalnizca ortam degiskeninden okunur, asla loglanmaz ve API
yanitlarina dahil edilmez. DRY_RUN=true iken zincire hicbir sey yazilmaz;
demo, fonlanmis cuzdan olmadan da uctan uca calisir.
"""
from __future__ import annotations

import json
from functools import lru_cache

from solders.keypair import Keypair
from solders.pubkey import Pubkey

from app.config import get_settings
from app.schemas import Currency, PaymentInstruction, TransferResult

settings = get_settings()


# --------------------------------------------------------------------------
# Cuzdan
# --------------------------------------------------------------------------
@lru_cache
def get_keypair() -> Keypair | None:
    """AGENT_WALLET_SECRET'i base58 veya JSON byte dizisi olarak cozer."""
    secret = (settings.agent_wallet_secret or "").strip()
    if not secret:
        return None
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


def agent_address() -> str | None:
    kp = get_keypair()
    return str(kp.pubkey()) if kp else None


def explorer_url(signature: str) -> str:
    return f"https://explorer.solana.com/tx/{signature}?cluster={settings.solana_cluster}"


# --------------------------------------------------------------------------
# Transfer
# --------------------------------------------------------------------------
async def execute_payment(payment: PaymentInstruction) -> TransferResult:
    """Onaylanmis odeme talimatini zincire yazar ve TxHash dondurur."""
    kp = get_keypair()

    if settings.dry_run or kp is None:
        reason = "DRY_RUN acik" if settings.dry_run else "operasyon cuzdani tanimli degil"
        return TransferResult(
            executed=False,
            simulated=True,
            tx_hash=f"SIMULATED-{payment.currency.value}-{payment.amount}",
            confirmed=False,
            error=f"Islem simule edildi ({reason}); zincire yazilmadi.",
        )

    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment import Confirmed

    client = AsyncClient(settings.solana_rpc_url, commitment=Confirmed)
    try:
        recipient = Pubkey.from_string(payment.recipient_wallet)
        if payment.currency == Currency.SOL:
            instructions = _sol_instructions(kp, recipient, payment.amount)
        else:
            instructions = _usdc_instructions(kp, recipient, payment.amount)

        signature = await _send(client, kp, instructions)
        confirmed = await _confirm(client, signature)
        return TransferResult(
            executed=True,
            simulated=False,
            tx_hash=str(signature),
            explorer_url=explorer_url(str(signature)),
            confirmed=confirmed,
            error=None if confirmed else "Islem gonderildi ancak onay beklemede.",
        )
    except Exception as exc:  # noqa: BLE001 - zincir hatasi karari bozmamali
        return TransferResult(executed=False, simulated=False, error=f"{type(exc).__name__}: {exc}")
    finally:
        await client.close()


def _sol_instructions(kp: Keypair, recipient: Pubkey, amount: float) -> list:
    from solders.system_program import TransferParams, transfer

    lamports = int(round(amount * 1_000_000_000))
    return [transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=recipient, lamports=lamports))]


def _usdc_instructions(kp: Keypair, recipient: Pubkey, amount: float) -> list:
    """SPL token transferi: alicinin ATA hesabi yoksa ayni islemde olusturur."""
    from spl.token.constants import TOKEN_PROGRAM_ID
    from spl.token.instructions import (
        create_idempotent_associated_token_account,
        get_associated_token_address,
        transfer_checked,
    )
    from spl.token.models import TransferCheckedParams

    mint = Pubkey.from_string(settings.usdc_mint)
    source_ata = get_associated_token_address(kp.pubkey(), mint)
    dest_ata = get_associated_token_address(recipient, mint)

    # Idempotent varyant: hesap zaten varsa islem basarisiz olmaz, yarisma
    # kosulu (biz kontrol ederken baskasinin ATA acmasi) ortadan kalkar.
    instructions = [
        create_idempotent_associated_token_account(
            payer=kp.pubkey(), owner=recipient, mint=mint
        )
    ]

    raw_amount = int(round(amount * (10 ** settings.usdc_decimals)))
    instructions.append(
        transfer_checked(
            TransferCheckedParams(
                program_id=TOKEN_PROGRAM_ID,
                source=source_ata,
                mint=mint,
                dest=dest_ata,
                owner=kp.pubkey(),
                amount=raw_amount,
                decimals=settings.usdc_decimals,
                signers=[],
            )
        )
    )
    return instructions


async def _send(client, kp: Keypair, instructions: list):
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    blockhash = (await client.get_latest_blockhash()).value.blockhash
    message = MessageV0.try_compile(
        payer=kp.pubkey(),
        instructions=instructions,
        address_lookup_table_accounts=[],
        recent_blockhash=blockhash,
    )
    tx = VersionedTransaction(message, [kp])
    return (await client.send_transaction(tx)).value


async def _confirm(client, signature) -> bool:
    """Zincirdeki islemin basari durumunu sorgular."""
    try:
        await client.confirm_transaction(signature, commitment="confirmed")
        return True
    except Exception:
        return False


async def wallet_status() -> dict:
    """Demo panelinde gosterilen cuzdan ozeti (gizli anahtar asla donmez)."""
    address = agent_address()
    status = {
        "address": address,
        "cluster": settings.solana_cluster,
        "rpc_url": settings.solana_rpc_url,
        "dry_run": settings.dry_run,
        "sol_balance": None,
        "usdc_balance": None,
    }
    if not address:
        return status

    from solana.rpc.async_api import AsyncClient

    client = AsyncClient(settings.solana_rpc_url)
    try:
        owner = Pubkey.from_string(address)
        status["sol_balance"] = (await client.get_balance(owner)).value / 1_000_000_000

        from spl.token.instructions import get_associated_token_address

        ata = get_associated_token_address(owner, Pubkey.from_string(settings.usdc_mint))
        try:
            bal = await client.get_token_account_balance(ata)
            status["usdc_balance"] = float(bal.value.ui_amount or 0.0)
        except Exception:
            status["usdc_balance"] = 0.0
    except Exception as exc:  # noqa: BLE001
        status["error"] = str(exc)
    finally:
        await client.close()
    return status
