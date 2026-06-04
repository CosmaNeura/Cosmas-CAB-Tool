import pandas as pd
import glob

files = glob.glob('datasets/*.json')

for file in files:
    print(file)
    # Load JSON file into a pandas DataFrame
    df = pd.read_json(file)

    # Write DataFrame to feather
    df.to_feather(file.strip('.json') + '.feather')