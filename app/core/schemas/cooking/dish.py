from app.core.schemas.base import SchemaBaseModel
from app.core.schemas.cooking.recipe import Dietary, Meta


# Tóm tắt dinh dưỡng dùng cho màn hình danh sách món ăn.
class DishNutritionSummary(SchemaBaseModel):
    calories: float
    protein_g: float
    carbohydrates_g: float
    fat_g: float


# Thông tin ngắn gọn của một món ăn trong kết quả tìm kiếm/danh sách.
class Dish(SchemaBaseModel):
    id: str
    name: str
    description: str
    category: str
    cuisine: str
    difficulty: str
    tags: list[str]
    meta: Meta
    dietary: Dietary
    nutrition_summary: DishNutritionSummary


# Phân trang và tổng số món trả về từ API danh sách.
class DishListMeta(SchemaBaseModel):
    total: int
    page: int
    per_page: int
    total_capped: bool


# Schema cấp cao nhất khớp với file list_chicken.json.
class DishListResponse(SchemaBaseModel):
    data: list[Dish]
    meta: DishListMeta


__all__ = [
    "Dish",
    "DishListMeta",
    "DishListResponse",
    "DishNutritionSummary",
]
