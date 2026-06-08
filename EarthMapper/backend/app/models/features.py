from pydantic import BaseModel, Field

class Geometry(BaseModel):
    type: str
    coordinates: list | float


class Feature(BaseModel):
    type: str = Field(default="Feature")
    geometry: Geometry
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class FeatureCollection(BaseModel):
    type: str = Field(default="FeatureCollection")
    features: list[Feature] = Field(default_factory=list)
