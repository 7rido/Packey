"""test_handlers.py — drive the real /status, /list, /info handlers with a
simulated private chat and print exactly what the bot would send (salvaged)."""
import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
import main
import refresh


class Chat:
    def __init__(self):
        self.id = 250444893
        self.type = "private"


class User:
    id = 250444893
    first_name = "Tester"


class Msg:
    def __init__(self, text):
        self.chat = Chat()
        self.from_user = User()
        self.text = text
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append(text)
        return type("Sent", (), {"message_id": 12345})()


async def run_command(text):
    msg = Msg(text)
    if text.startswith("/status"):
        await main.status_handler(msg)
    elif text.startswith("/list"):
        await main.list_handler(msg)
    elif text.startswith("/info "):
        await main.info_handler(msg)
    return msg


async def main_():
    main.load_data()
    # The cycle timestamp lives in refresh.py (main's copy went away in Session
    # 7a): writing main.LAST_CYCLE_AT only created an attribute nothing reads,
    # so /status still printed "never".
    refresh.LAST_CYCLE_AT = main.now_iso()
    print("=" * 60)
    print("SIMULATED: /status")
    print("=" * 60)
    msg = await run_command("/status")
    print(msg.sent[0] if msg.sent else "(no reply)")
    print()

    print("=" * 60)
    print("SIMULATED: /list  (summary + first card)")
    print("=" * 60)
    msg = await run_command("/list")
    print("-- summary --\n" + msg.sent[0])
    print("\n-- first card --\n" + msg.sent[1])
    print("\n-- messages sent:", len(msg.sent))

    print()
    print("=" * 60)
    print("SIMULATED: /info <Out-for-Delivery id>")
    print("=" * 60)
    msg = await run_command("/info 9300111043900026403077")
    for m in msg.sent:
        print(m[:400])


asyncio.run(main_())