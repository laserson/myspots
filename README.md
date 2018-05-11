# myspots


```bash
python cli.py extract-kml-placemarks ny.kml \
    | python cli.py batch-query-placemarks --suffix nyc - ny.json
```
