"""Compile an unsigned Solana transaction for one Pro payment (hand-rolled).

Why this exists (2026-09-24): the pay page used to hand the wallet a `solana:`
transfer-request URI. That only works when the OS routes the scheme to the wallet the
payer chose — and every Solana wallet registers `solana:`, so the device's DEFAULT
handler wins. A payer whose default is Coinbase Wallet got that app's own empty send
screen: address and amount gone (reported from the field; Phantom's own "Request
payment" recipe is the same URL, so no wallet-scoped payment link exists to use
instead). The one route that is wallet-scoped by construction is to hand the wallet a
TRANSACTION: no scheme is resolved, because the page calls the wallet provider that is
already injected in the in-app browser. That is also how Phantom's docs build a
payment (build a transaction, then signAndSendTransaction).

The balancing force is that the transaction must be built without a Solana SDK, so
this module IS the wire format and nothing else: no network, no keys, no state. It
never sees a private key — the transaction leaves here with a zero-filled signature
slot, exactly what `Transaction.serialize({requireAllSignatures: false})` produces and
what a wallet expects to fill in.

What a transaction is, byte for byte:
    shortvec(signature count) || 64-byte signature slots || message
and the message is:
    header (3 bytes: required signatures, readonly SIGNERS, readonly UNSIGNED)
    || shortvec(account count) || 32-byte accounts
    || 32-byte recent blockhash
    || shortvec(instruction count) || instructions
an instruction being: program id INDEX (one byte), shortvec(account count), account
indices (one byte each), shortvec(data length), data.

Accounts are ordered by the MESSAGE, not by any instruction: the fee payer first, then
other writable signers, readonly signers, writable non-signers, and finally readonly
non-signers. The compiler below derives that order from what the instructions ask for.
That derivation is the part worth being paranoid about: instructions address accounts
by INDEX, so one key in the wrong slot moves tokens from or to the wrong account — and
a transaction is signed blind by the user's wallet. So the tests pin the exact bytes
for a hand-computed case, and the account table is built by key (not by position) so a
duplicate key can never occupy two slots.
"""
import base64
from typing import Dict, List, Sequence, Tuple

# Base58's alphabet, shared with pro._b58encode (that one encodes, this one decodes).
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"

# SPL Token instruction discriminators (the enum's indices, which are the wire values).
TRANSFER_CHECKED = 12

# A payment transaction has exactly one signer: the payer pays the fee. The compiler
# refuses anything else rather than emitting a message whose header count would not
# match the number of signature slots we write — a mismatch the wallet would reject.
_SIGNERS = 1


def b58decode(text: str) -> bytes:
    """Base58 (Bitcoin alphabet) to bytes.

    Hand-rolled for the same reason pro._b58encode is: one direction of one alphabet
    is not worth a dependency. An illegal character raises instead of being skipped —
    a silently truncated address is a payment to nowhere.
    """
    number = 0
    for char in text:
        index = _B58.find(char)
        if index < 0:
            raise ValueError("not base58")
        number = number * 58 + index
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    # A leading '1' is a leading zero byte, which int() has no way to remember.
    return b"\x00" * (len(text) - len(text.lstrip("1"))) + body


def b58encode(data: bytes) -> str:
    """Bytes to base58 (Bitcoin alphabet) — the mirror of b58decode above.

    Here rather than in each caller because the phantom deep-link payloads need the
    same encoding the wallet uses for addresses and keys; pro._b58encode predates
    this module and is left where it is (its tests pin it).
    """
    zeros = len(data) - len(data.lstrip(b"\x00"))
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, remainder = divmod(number, 58)
        out = _B58[remainder] + out
    return "1" * zeros + out


def pubkey(address: str) -> bytes:
    """A base58 address as its 32 raw bytes. Raises ValueError if it is not one."""
    raw = b58decode(address)
    if len(raw) != 32:
        raise ValueError("not a 32-byte public key")
    return raw


def shortvec(count: int) -> bytes:
    """Solana's compact-u16 length prefix: 7 bits per byte, high bit means "more"."""
    if count < 0:
        raise ValueError("negative length")
    out = bytearray()
    while True:
        byte = count & 0x7F
        count >>= 7
        if not count:
            out.append(byte)
            return bytes(out)
        out.append(byte | 0x80)


def _compile_transaction(fee_payer: bytes,
                         accounts: Sequence[Tuple[bytes, bool, bool]],
                         instructions: Sequence[Tuple[bytes, Sequence[int], bytes]],
                         blockhash: bytes) -> bytes:
    """Serialize an UNSIGNED legacy transaction.

    `accounts` are (key, is_signer, is_writable) as the instructions want them, and
    may repeat a key; each key ends up in the message exactly once, with the union of
    the flags asked of it. `instructions` are (program key, [indices into `accounts`],
    data) — indices, not keys, because the wire format is positional. The program key
    is looked up in the compiled table like any other account.
    """
    flags: Dict[bytes, List[bool]] = {}
    for key, signer, writable in accounts:
        if len(key) != 32:
            raise ValueError("account key must be 32 bytes")
        entry = flags.setdefault(key, [False, False])
        entry[0] = entry[0] or bool(signer)
        entry[1] = entry[1] or bool(writable)
    if fee_payer not in flags:
        raise ValueError("fee payer is not one of the accounts")
    # The fee payer signs and pays, whatever an instruction said about it.
    flags[fee_payer] = [True, True]

    writable_signers = [k for k, f in flags.items() if f[0] and f[1]]
    readonly_signers = [k for k, f in flags.items() if f[0] and not f[1]]
    writable = [k for k, f in flags.items() if not f[0] and f[1]]
    readonly = [k for k, f in flags.items() if not f[0] and not f[1]]
    ordered = ([fee_payer] + [k for k in writable_signers if k != fee_payer]
               + readonly_signers + writable + readonly)
    if len(ordered) > 255:
        raise ValueError("too many accounts for one-byte indices")
    if len(writable_signers) + len(readonly_signers) != _SIGNERS:
        raise ValueError("a payment transaction has exactly one signer")

    index = {key: position for position, key in enumerate(ordered)}
    message = bytearray()
    message += bytes([len(writable_signers) + len(readonly_signers),
                      len(readonly_signers), len(readonly)])
    message += shortvec(len(ordered))
    for key in ordered:
        message += key
    message += blockhash
    message += shortvec(len(instructions))
    for program, positions, data in instructions:
        try:
            program_index = index[program]
            account_indices = [index[accounts[position][0]] for position in positions]
        except (KeyError, IndexError):
            raise ValueError("instruction references an unknown account")
        if len(account_indices) > 255:
            raise ValueError("too many accounts in one instruction")
        message.append(program_index)
        message += shortvec(len(account_indices))
        message += bytes(account_indices)
        message += shortvec(len(data))
        message += data

    # One signature slot, zero-filled: the wallet fills it. `requireAllSignatures:
    # false` in web3.js terms — nobody here holds a key.
    return shortvec(_SIGNERS) + b"\x00" * 64 + bytes(message)


def payment_transaction(*, payer: str, source: str, destination: str, mint: str,
                        amount_micros: int, decimals: int, memo: str,
                        reference: str = "", blockhash: str) -> bytes:
    """The unsigned transaction for one Pro payment, ready for the payer's wallet.

    `source` is the payer's token account for `mint` and `destination` is ours — both
    looked up from the chain by the caller (the RPC answers that question, so nothing
    here needs the ed25519 on-curve check a hand-rolled ATA derivation would, and the
    decimals come from the same lookup instead of being assumed).

    `TransferChecked` rather than a bare `Transfer`: the token program then verifies
    the mint and the decimals on-chain, so a mixed-up mint cannot spend the wrong
    token. It is the instruction Solana Pay requires of a wallet for the same reason.

    The transfer is the LAST instruction (Solana Pay's requirement, and what our own
    monitor reads as the payment), after the memo when the invoice has one. The
    reference — when the invoice has one — rides along as a read-only, non-signing
    account of the transfer, which is the spec's placement for it and the strongest way
    for the monitor to match the payment to its invoice.
    """
    if not 0 < amount_micros < 2 ** 64:
        raise ValueError("amount must be a positive u64 of base units")
    if not 0 <= decimals <= 255:
        raise ValueError("decimals must fit a byte")
    if len(memo.encode("utf-8")) > 566:              # the memo program's limit
        raise ValueError("memo must be at most 566 bytes")

    token_program = pubkey(TOKEN_PROGRAM)
    memo_program = pubkey(MEMO_PROGRAM)
    accounts: List[Tuple[bytes, bool, bool]] = [
        (pubkey(payer), True, True),        # fee payer and the transfer's authority
        (pubkey(source), False, True),
        (pubkey(mint), False, False),
        (pubkey(destination), False, True),
        (token_program, False, False),
        (memo_program, False, False),
    ]
    # Positions in `accounts`, resolved to message indices by the compiler.
    transfer_positions = [1, 2, 3, 0]
    if reference:
        accounts.append((pubkey(reference), False, False))
        transfer_positions.append(6)
    data = (bytes([TRANSFER_CHECKED]) + amount_micros.to_bytes(8, "little")
            + bytes([decimals]))
    instructions: List[Tuple[bytes, Sequence[int], bytes]] = []
    if memo:
        # The memo is a matching hint, never the payment itself, so an invoice without
        # one simply carries no memo instruction — a transfer needs none. The memo
        # program stays in the account list either way: an unreferenced account is
        # legal, and keeping the slots fixed is what makes the indices above stable.
        instructions.append((memo_program, [], memo.encode("utf-8")))
    instructions.append((token_program, transfer_positions, data))
    return _compile_transaction(pubkey(payer), accounts, instructions,
                                pubkey(blockhash))


def to_base64(transaction: bytes) -> str:
    """What the page hands the wallet provider (base64 is what both spellings take)."""
    return base64.b64encode(transaction).decode("ascii")
