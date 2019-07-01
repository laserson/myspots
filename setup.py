from setuptools import setup

setup(
    name="myspots",
    version="0.0.0",
    author="Uri Laserson",
    author_email="uri.laserson@gmail.com",
    description="Search Google Maps for places and push to Airtable",
    url="https://github.com/laserson/myspots",
    install_requires=["click", "pyyaml", "fastkml", "googlemaps", "airtable"],
    py_modules=["myspots"],
    entry_points={"console_scripts": ["myspots = myspots:cli"]},
)
