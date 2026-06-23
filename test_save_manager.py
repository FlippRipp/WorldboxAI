import os
import shutil
from pathlib import Path
from backend.engine.save_manager import SaveManager

def test_save_system():
    print("Testing Save System (Phase 4)...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "test_data")
    
    # Cleanup old test data
    if os.path.exists(data_dir):
        shutil.rmtree(data_dir)
        
    sm = SaveManager(data_dir)
    
    # 1. Create a player template
    print("1. Creating player template...")
    p_data = {
        "name": "Garrick",
        "module_data": {
            "wb_core_rpg": {"stats": {"strength": 16, "dexterity": 10, "constitution": 12, "intelligence": 10, "wisdom": 10, "charisma": 10}, "level": 1, "hp": 20, "max_hp": 20}
        }
    }
    sm.create_player_template("garrick", p_data)
    
    loaded_p = sm.load_player_template("garrick")
    assert loaded_p["name"] == "Garrick"
    
    # 2. Create a new save instance (.wbx)
    print("2. Creating new save instance...")
    initial_state = {
        "module_data": {
            "wb_core_rpg": {"stats": {"constitution": 12, "strength": 16, "dexterity": 10, "intelligence": 10, "wisdom": 10, "charisma": 10}, "level": 1, "hp": 85, "max_hp": 85}
        }
    }
    sm.create_new_save("save1", ["garrick"], initial_state)
    
    assert os.path.exists(os.path.join(data_dir, "saves", "save1.wbx"))
    
    # 3. Load the save
    print("3. Loading save...")
    active_state = sm.load_save("save1")
    assert active_state["core"]["metadata"]["turn"] == 0
    assert "garrick" in active_state["characters"]
    assert "wb_core_rpg" in active_state["module_data"]
    
    # 4. Save a turn and create snapshot
    print("4. Saving turn 1...")
    active_state["history"] = ["Turn 1 happened."]
    active_state["characters"]["garrick"]["module_data"]["wb_core_rpg"]["hp"] -= 5
    active_state["module_data"]["wb_core_rpg"]["hp"] = active_state["characters"]["garrick"]["module_data"]["wb_core_rpg"]["hp"]
    
    sm.save_turn("save1", active_state, 1)
    
    # 5. Save another turn
    print("5. Saving turn 2...")
    active_state["history"].append("Turn 2 happened.")
    active_state["characters"]["garrick"]["module_data"]["wb_core_rpg"]["hp"] -= 2
    sm.save_turn("save1", active_state, 2)
    
    # 6. Test Undo Turn
    print("6. Testing Undo to Turn 1...")
    restored_state = sm.undo_turn("save1", 1)
    assert restored_state["core"]["metadata"]["turn"] == 1
    assert restored_state["characters"]["garrick"]["module_data"]["wb_core_rpg"]["hp"] == 15
    
    print("All tests passed! Phase 4 is functionally complete.")

if __name__ == "__main__":
    test_save_system()
