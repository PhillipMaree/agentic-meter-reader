import asyncio

from src.agents.meter import run


async def main() -> None:
    await run()


if __name__ == "__main__":
    asyncio.run(main())
