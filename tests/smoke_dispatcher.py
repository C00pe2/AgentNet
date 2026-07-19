"""Quick smoke test for dispatcher."""
from app.services.dispatcher import _topo_layers

# Test 1: linear chain 0 -> 1 -> 2
steps = [
    {"step_id": 0, "depends_on": []},
    {"step_id": 1, "depends_on": [0]},
    {"step_id": 2, "depends_on": [0, 1]},
]
layers = _topo_layers(steps)
for i, layer in enumerate(layers):
    print(f"[linear] layer {i}: {[s['step_id'] for s in layer]}")
assert [s['step_id'] for s in layers[0]] == [0]
assert [s['step_id'] for s in layers[1]] == [1]
assert [s['step_id'] for s in layers[2]] == [2]
print("OK linear")

# Test 2: diamond 0 -> {1, 2} -> 3
steps2 = [
    {"step_id": 0, "depends_on": []},
    {"step_id": 1, "depends_on": [0]},
    {"step_id": 2, "depends_on": [0]},
    {"step_id": 3, "depends_on": [1, 2]},
]
layers2 = _topo_layers(steps2)
for i, layer in enumerate(layers2):
    print(f"[diamond] layer {i}: {[s['step_id'] for s in layer]}")
assert [s['step_id'] for s in layers2[0]] == [0]
assert sorted(s['step_id'] for s in layers2[1]) == [1, 2]
assert [s['step_id'] for s in layers2[2]] == [3]
print("OK diamond")

# Test 3: independent
steps3 = [
    {"step_id": 0, "depends_on": []},
    {"step_id": 1, "depends_on": []},
    {"step_id": 2, "depends_on": []},
]
layers3 = _topo_layers(steps3)
for i, layer in enumerate(layers3):
    print(f"[indep] layer {i}: {[s['step_id'] for s in layer]}")
assert sorted(s['step_id'] for s in layers3[0]) == [0, 1, 2]
print("OK independent")

# Test 4: cycle detection
try:
    _topo_layers([
        {"step_id": 0, "depends_on": [1]},
        {"step_id": 1, "depends_on": [0]},
    ])
    print("FAIL: cycle was not detected")
except ValueError as e:
    print(f"OK cycle detected: {e}")
