from pydantic import BaseModel,Field,ConfigDict
class Clean(BaseModel):model_config=ConfigDict(str_strip_whitespace=True)
class DriftClear(Clean):
    reason:str=Field(min_length=8,max_length=600)
