import re
import os

FILE_PATH = "core/engine/symbol_engine.py"

METHODS_TO_ASYNC = [
    "save_state",
    "load_state",
    "_log_trade",
    "_execute_market_order",
    "_execute_trade_with_chain",
    "_check_virtual_triggers",
    "_check_graceful_stop_complete",
    "_create_expansion_pair",
    "_create_next_positive_pair",
    "_create_next_negative_pair",
    "_check_and_expand",
    "_check_tp_sl_from_history", # Accesses DB? user plan says "Query MT5 for deals...". 
    # Plan says "_check_tp_sl_from_history scans processed_deals... Fix: Query MT5... Update DB".
    # So _check_tp_sl_from_history will likely need DB, so async.
]

def fix_await():
    with open(FILE_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Update Method Definitions to async def
    for method in METHODS_TO_ASYNC:
        # Regex to find 'def method_name(' and ensure it's not already 'async def'
        # We look for "def method_name" and replace with "async def method_name"
        # But handle indentation
        pattern = r"(^[ \t]*)def\s+" + re.escape(method) + r"\("
        replacement = r"\1async def " + method + "("
        content = re.sub(pattern, replacement, content, flags=re.MULTILINE)

    # 2. Update Call Sites to await self.method()
    # Be careful not to double await or await definition
    for method in METHODS_TO_ASYNC:
        # Look for 'self.method(' that is NOT preceded by 'await ' and NOT 'def '
        # Negative lookbehind is hard with variable length spaces, 
        # so we match 'self.method(' and check prefix
        
        # Regex: (anything but 'await ' and 'def ')\bself\.method\(
        # We'll use a callback to be safe
        pattern = r"([^a-zA-Z0-9_])self\." + re.escape(method) + r"\("
        
        def repl(match):
            prefix = match.group(1)
            # Check if prefix contains 'await' or 'def' (scanning backwards in prefix is tricky if just char)
            # Actually the regex group 1 is just the preceding char.
            # If it's part of 'await ', e.g. 't', it matched 'tself.method('. 
            # Wait, 'await self.method(' -> 't' is ' ' or 't'.
            
            # Better approach: Match strictly 'await self.method' or 'def method'.
            # If not matched, replace.
            
            # Simple heuristic:
            # Replace 'self.method(' with 'await self.method(' globally
            # Then fix double awaits 'await await' -> 'await'
            # Then fix 'def await' -> 'def' (wait, definitions don't use self.)
            return f"{prefix}await self.{method}("

        content = re.sub(pattern, repl, content)

    # 3. Clean up generic artifacts
    # 'await await' -> 'await'
    content = content.replace("await await ", "await ")
    
    # 'async async def' -> 'async def' (if we added async to already async method)
    content = content.replace("async async def", "async def")

    with open(FILE_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print("Successfully updated async/await calls in symbol_engine.py")

if __name__ == "__main__":
    if os.path.exists(FILE_PATH):
        fix_await()
    else:
        print(f"File not found: {FILE_PATH}")
