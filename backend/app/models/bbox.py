from pydantic import BaseModel, model_validator


class BBox(BaseModel):
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.min_lon >= self.max_lon or self.min_lat >= self.max_lat:
            raise ValueError("Min value should be lower than max value.")
        return self
    
    def as_tuple(self):
        return (self.min_lon, self.min_lat, self.max_lon, self.max_lat)




