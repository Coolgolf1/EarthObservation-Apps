from pydantic import BaseModel, Field, model_validator


class BBox(BaseModel):
    min_lon: float = Field(ge=-180, le=180)
    min_lat: float = Field(ge=-90, le=90)
    max_lon: float = Field(ge=-180, le=180)
    max_lat: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min_lon >= self.max_lon or self.min_lat >= self.max_lat:
            raise ValueError("Min value should be lower than max value.")
        return self
    
    def as_tuple(self):
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)




