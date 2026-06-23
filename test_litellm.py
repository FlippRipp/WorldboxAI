import asyncio
from litellm import acompletion
import os
from dotenv import load_dotenv

load_dotenv("backend/.env")

async def test_gemini():
    models_to_test = ["gemini/gemini-2.5-flash", "gemini/gemini-3.5-flash"]
    for model in models_to_test:
        try:
            print(f"Testing {model}...")
            response = await acompletion(
                model=model,
                messages=[{"role": "user", "content": "Hello"}],
                stream=False
            )
            print(f"Success for {model}: {response.choices[0].message.content[:20]}")
            return
        except Exception as e:
            print(f"Failed for {model}: {e}")

asyncio.run(test_gemini())