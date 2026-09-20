from datasets import load_dataset

print("Загружаю датасет...")
ds = load_dataset("irlspbru/RusLawOD", split="train", streaming=False)

print(f"\nКоличество документов: {len(ds)}")
print(f"\nКолонки: {ds.column_names}")

print("\n--- Первый документ ---")
row = ds[0]
for key, val in row.items():
    val_str = str(val)
    print(f"  {key}: {val_str[:200]}")
