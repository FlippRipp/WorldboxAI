import asyncio
from backend.engine.registry import ModuleRegistry
from backend.engine.graph import EngineGraph
from dotenv import load_dotenv
import os

base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(base_dir, "backend", ".env"))

async def main():
    print("Testing Engine Directly...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    modules_dir = os.path.join(base_dir, "modules")
    registry = ModuleRegistry(modules_dir)
    registry.load_all_modules()
    engine = EngineGraph(registry)
    
    async def stream_token(token: str):
        print(token, end="", flush=True)
        
    engine.sdk.ui.on_token = stream_token
    
    game_state = {
        "input_text": "I swing my sword",
        "module_data": {
            "wb_core_rpg": {"hp": 85, "max_hp": 85}
        },
        "current_context": [],
        "history": []
    }
    
    try:
        final_state = await engine.app.ainvoke(game_state)
        print("\n\nFinal State:")
        import pprint
        pprint.pprint(final_state)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())