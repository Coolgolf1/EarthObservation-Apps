# Databricks notebook source
# MAGIC %md
# MAGIC # Time Service Example Notebook
# MAGIC
# MAGIC This notebook uses API-like functions from time_service.py (without creating a real API server).
# MAGIC
# MAGIC It demonstrates two endpoint-style calls:
# MAGIC 1. Data endpoint: travel-time + category data + ESRI counts
# MAGIC 2. Map endpoint: visualization for the same payload

# COMMAND ----------

import importlib
import matplotlib.pyplot as plt
import pandas as pd

import time_service
importlib.reload(time_service)

from time_service import endpoint_time_features, endpoint_esri_feature_counts, endpoint_render_map

# COMMAND ----------

payload = endpoint_time_features(
    place_query="Plaza de la Virgen, Valencia",
    minutes=40,
    profile="driving-car",
    country_hint="ESP",
    context_fetch_padding_ratio=0.8,
 )

print("Center:", payload["center_name"] )
print("Time (minutes):", payload["minutes"] )
print("Iso source:", payload["iso_source"] )
print("BBox:", payload["bbox"] )
print("Context bbox:", payload["context_bbox"] )

counts = endpoint_esri_feature_counts(payload)
print("\nESRI feature counts by category:")
print(counts)

display(payload["summary"])

# COMMAND ----------

fig = endpoint_render_map(
    payload,
    title="Valencia 40-minute reach area",
    context_padding_ratio=0.65,
 )
plt.show()

# COMMAND ----------

payload2 = endpoint_time_features(
    place_query="Plaza Nueva, Sevilla",
    minutes=35,
    profile="driving-car",
    country_hint="ESP",
)

print("Second location center:", payload2["center_name"])
print("Second location ESRI counts:", endpoint_esri_feature_counts(payload2))
display(payload2["summary"])

fig2 = endpoint_render_map(payload2, title="Sevilla 35-minute reach area")
plt.show()

# COMMAND ----------

payload3 = endpoint_time_features(
    place_query="Statue of Liberty",
    minutes=60,
    profile="driving-car",
    country_hint="USA",
)

print("Second location center:", payload3["center_name"])
print("Second location ESRI counts:", endpoint_esri_feature_counts(payload3))
display(payload3["summary"])

fig2 = endpoint_render_map(payload3, title="Statue of Liberty 60-minute reach area")
plt.show()