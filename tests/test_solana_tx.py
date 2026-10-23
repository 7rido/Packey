"""The hand-rolled Solana transaction builder.

Why the bytes are pinned here: the transaction is signed BLIND by the payer's wallet,
and instructions address accounts by index, so a wrong slot means money moved to or
from the wrong account. The module has no Solana SDK to lean on, so these tests read
the format back with their own parser and assert the exact wire bytes for a
hand-computed case.
"""
import base64

import pytest

import pro
import solana_tx as stx


def _key(byte_value):
    """A 32-byte account key, distinguishable by its first byte."""
    return bytes([byte_value]) * 32


BLOCKHASH = _key(9)


def _read_shortvec(raw, at):
    value = 0
    shift = 0
    while True:
        byte = raw[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def _parse(transaction):
    """Minimal independent reader of a legacy transaction: the module's own work is
    never used to check the module."""
    signatures, at = _read_shortvec(transaction, 0)
    slots = [transaction[at + 64 * i:at + 64 * (i + 1)] for i in range(signatures)]
    at += 64 * signatures
    required, readonly_signers, readonly_unsigned = transaction[at:at + 3]
    at += 3
    count, at = _read_shortvec(transaction, at)
    keys = [transaction[at + 32 * i:at + 32 * (i + 1)] for i in range(count)]
    at += 32 * count
    blockhash = transaction[at:at + 32]
    at += 32
    instruction_count, at = _read_shortvec(transaction, at)
    instructions = []
    for _ in range(instruction_count):
        program = transaction[at]
        at += 1
        accounts, at = _read_shortvec(transaction, at)
        indices = list(transaction[at:at + accounts])
        at += accounts
        size, at = _read_shortvec(transaction, at)
        data = transaction[at:at + size]
        at += size
        instructions.append((program, indices, data))
    assert at == len(transaction), "trailing bytes: the layout is wrong"
    return {"signatures": slots,
            "header": (required, readonly_signers, readonly_unsigned),
            "keys": keys, "blockhash": blockhash, "instructions": instructions}


# ── base58 and length prefixes ────────────────────────────────────────────────

def test_shortvec_matches_solanas_compact_u16():
    assert stx.shortvec(0) == b"\x00"
    assert stx.shortvec(1) == b"\x01"
    assert stx.shortvec(127) == b"\x7f"
    assert stx.shortvec(128) == b"\x80\x01"
    assert stx.shortvec(300) == b"\xac\x02"
    assert stx.shortvec(16383) == b"\xff\x7f"
    assert stx.shortvec(16384) == b"\x80\x80\x01"


def test_base58_round_trips_against_the_encoder_pro_already_has():
    for raw in (b"\x00", b"\x00\x00\x01", b"solana pay", bytes(range(32))):
        assert stx.b58decode(pro._b58encode(raw)) == raw


def test_base58_keeps_leading_zero_bytes_as_ones():
    assert stx.b58decode("1") == b"\x00"
    assert stx.b58decode("111") == b"\x00\x00\x00"
    assert stx.b58decode("1" + pro._b58encode(b"\x01")) == b"\x00\x01"


def test_the_known_programs_decode_to_their_documented_bytes():
    """Two public constants, straight from the token/memo program docs: if the decoder
    were subtly wrong, these would not come out."""
    assert stx.pubkey(stx.TOKEN_PROGRAM).hex() == (
        "06ddf6e1d765a193d9cbe146ceeb79ac1cb485ed5f5b37913a8cf5857eff00a9")
    assert stx.pubkey(stx.MEMO_PROGRAM).hex() == (
        "054a535a992921064d24e87160da387c7c35b5ddbc92bb81e41fa8404105448d")


@pytest.mark.parametrize("bad", ["0OIl", "l", " ", "abc!"])
def test_an_illegal_base58_character_is_fatal(bad):
    """A silently truncated address would be a payment to nowhere."""
    with pytest.raises(ValueError):
        stx.b58decode(bad)


def test_an_empty_address_never_reaches_the_compiler():
    assert stx.b58decode("") == b""
    with pytest.raises(ValueError):
        stx.pubkey("")


def test_an_address_of_the_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        stx.pubkey(pro._b58encode(b"\x01" * 31))
    with pytest.raises(ValueError):
        stx.pubkey(pro._b58encode(b"\x01" * 33))


# ── the message compiler ─────────────────────────────────────────────────────

def test_the_message_layout_is_the_documented_wire_format():
    """One hand-computed case, byte for byte: header, account order (fee payer, then
    writable, then readonly), blockhash, one instruction with indices."""
    payer, writable, readonly, program = _key(1), _key(2), _key(3), _key(4)
    raw = stx._compile_transaction(
        payer,
        [(payer, True, True), (writable, False, True),
         (readonly, False, False), (program, False, False)],
        [(program, [1, 2], b"\x01\x02")],
        BLOCKHASH,
    )
    expected = (stx.shortvec(1) + b"\x00" * 64
                + b"\x01\x00\x02"                      # 1 signature, 0 readonly signers,
                + b"\x04"                              # 2 readonly unsigned accounts
                + payer + writable + readonly + program
                + BLOCKHASH
                + b"\x01"                              # one instruction
                + bytes([3]) + b"\x02" + bytes([1, 2]) + b"\x02" + b"\x01\x02")
    assert raw == expected


def test_the_transaction_is_unsigned():
    """One zero-filled slot: the wallet fills it, nobody here holds a key."""
    parsed = _parse(stx._compile_transaction(
        _key(1), [(_key(1), True, True)], [], BLOCKHASH))
    assert parsed["signatures"] == [b"\x00" * 64]
    assert parsed["header"][0] == 1


def test_a_key_asked_for_twice_takes_one_slot():
    """The fee payer is also the transfer's authority: one message account, not two —
    a duplicate would shift every later index."""
    raw = stx._compile_transaction(
        _key(1),
        [(_key(1), True, True), (_key(2), False, True), (_key(1), False, False)],
        [(_key(2), [0, 1], b"\x00")],
        BLOCKHASH,
    )
    parsed = _parse(raw)
    assert parsed["keys"] == [_key(1), _key(2)]
    assert parsed["header"] == (1, 0, 0), "both non-signers are writable"
    assert parsed["instructions"][0][1] == [0, 1]


def test_the_compiler_refuses_a_second_signer_or_a_foreign_fee_payer():
    """We write the signature slot count and the compiler writes the header: a
    disagreement is a transaction the wallet would reject."""
    with pytest.raises(ValueError):
        stx._compile_transaction(
            _key(1), [(_key(1), True, True), (_key(2), True, False)], [], BLOCKHASH)
    with pytest.raises(ValueError):
        stx._compile_transaction(_key(7), [(_key(1), True, True)], [], BLOCKHASH)


def test_the_compiler_refuses_a_short_or_unknown_account():
    with pytest.raises(ValueError):
        stx._compile_transaction(
            _key(1), [(_key(1), True, True), (b"\x01", False, False)], [], BLOCKHASH)
    with pytest.raises(ValueError):
        stx._compile_transaction(_key(1), [(_key(1), True, True)],
                                 [(_key(4), [0], b"\x00")], BLOCKHASH)
    with pytest.raises(ValueError):
        stx._compile_transaction(_key(1), [(_key(1), True, True)],
                                 [(_key(1), [3], b"\x00")], BLOCKHASH)


# ── the payment transaction ──────────────────────────────────────────────────

def _payment(**overrides):
    args = dict(payer=pro._b58encode(_key(1)), source=pro._b58encode(_key(2)),
                destination=pro._b58encode(_key(3)), mint=pro._b58encode(_key(4)),
                amount_micros=16055, decimals=6, memo="pkabc123",
                reference=pro._b58encode(_key(5)), blockhash=pro._b58encode(BLOCKHASH))
    args.update(overrides)
    return stx.payment_transaction(**args)


def test_the_transfer_is_last_the_amount_is_right_and_the_reference_rides_along():
    parsed = _parse(_payment())
    assert parsed["header"] == (1, 0, 4), "mint, two programs and the reference"
    memo_instruction, transfer = parsed["instructions"]
    assert parsed["keys"][memo_instruction[0]] == stx.pubkey(stx.MEMO_PROGRAM)
    assert memo_instruction[1] == [], "the memo carries no accounts"
    assert memo_instruction[2] == b"pkabc123"
    assert parsed["keys"][transfer[0]] == stx.pubkey(stx.TOKEN_PROGRAM)
    # source, mint, destination, owner — then the reference, read-only.
    assert [parsed["keys"][i] for i in transfer[1]] == [
        stx.pubkey(pro._b58encode(_key(2))), stx.pubkey(pro._b58encode(_key(4))),
        stx.pubkey(pro._b58encode(_key(3))), stx.pubkey(pro._b58encode(_key(1))),
        stx.pubkey(pro._b58encode(_key(5)))]
    assert transfer[2] == b"\x0c" + (16055).to_bytes(8, "little") + b"\x06", (
        "TransferChecked, the exact micros, the mint's decimals")


def test_without_a_reference_the_transfer_has_four_accounts():
    parsed = _parse(_payment(reference=""))
    assert parsed["header"] == (1, 0, 3)
    assert len(parsed["instructions"][1][1]) == 4


def test_the_payer_is_the_first_account_and_the_only_signer():
    parsed = _parse(_payment())
    assert parsed["keys"][0] == stx.pubkey(pro._b58encode(_key(1)))
    assert parsed["header"][0] == 1
    assert parsed["signatures"] == [b"\x00" * 64]


def test_the_blockhash_is_carried_verbatim():
    assert _parse(_payment())["blockhash"] == BLOCKHASH


def test_a_memo_less_payment_is_still_a_valid_transfer():
    """The memo is a matching aid, not the payment: a transfer needs no memo
    instruction, and no invoice shape may make one unpayable."""
    parsed = _parse(_payment(memo=""))
    assert len(parsed["instructions"]) == 1, "no memo instruction"
    program, accounts, data = parsed["instructions"][0]
    assert parsed["keys"][program] == stx.pubkey(stx.TOKEN_PROGRAM)
    assert [parsed["keys"][i] for i in accounts[:4]] == [
        stx.pubkey(pro._b58encode(_key(2))), stx.pubkey(pro._b58encode(_key(4))),
        stx.pubkey(pro._b58encode(_key(3))), stx.pubkey(pro._b58encode(_key(1)))]
    assert data == b"\x0c" + (16055).to_bytes(8, "little") + b"\x06"


def test_to_base64_is_what_a_wallet_provider_takes():
    raw = _payment()
    assert base64.b64decode(stx.to_base64(raw)) == raw


@pytest.mark.parametrize("bad", [
    {"amount_micros": 0},
    {"amount_micros": 2 ** 64},
    {"decimals": -1},
    {"decimals": 256},
    {"memo": "x" * 567},
    {"source": "not-base58!"},
    {"reference": "tok"},                       # too short to be a pubkey
])
def test_the_builder_refuses_anything_it_cannot_express(bad):
    with pytest.raises(ValueError):
        _payment(**bad)
