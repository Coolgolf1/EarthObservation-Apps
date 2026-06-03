from typing import Any

from pydantic import BaseModel, Field

class Geometry(BaseModel):
    type: str
    coordinates: Any


class Feature(BaseModel):
    type: str = Field(default="Feature")
    geometry: Geometry
    properties: dict[str, Any] = Field(default_factory=dict)


class FeatureCollection(BaseModel):
    type: str = Field(default="FeatureCollection")
    features: list[Feature] = Field(default_factory=list)