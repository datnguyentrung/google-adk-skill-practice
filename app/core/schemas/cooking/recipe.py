from pydantic import Field

from app.core.schemas.base import SchemaBaseModel


# Thông tin thời gian, khẩu phần và sản lượng của công thức.
class Meta(SchemaBaseModel):
    active_time: str
    passive_time: str
    total_time: str
    overnight_required: bool
    yields: str
    yield_count: int
    serving_size_g: int


# Các nhãn ăn kiêng và đối tượng không phù hợp.
class Dietary(SchemaBaseModel):
    flags: list[str]
    not_suitable_for: list[str]


# Thời hạn bảo quản cho từng môi trường.
class StorageMethod(SchemaBaseModel):
    notes: str
    duration: str


# Cách bảo quản và hâm nóng món ăn.
class Storage(SchemaBaseModel):
    refrigerator: StorageMethod
    freezer: StorageMethod
    reheating: str
    does_not_keep: bool


# Dụng cụ cần dùng khi nấu, kèm lựa chọn thay thế nếu có.
class Equipment(SchemaBaseModel):
    name: str
    required: bool
    alternative: str | None


# Nguyên liệu đơn lẻ trong một nhóm nguyên liệu.
class Ingredient(SchemaBaseModel):
    name: str
    quantity: float
    unit: str | None
    preparation: str | None
    notes: str | None
    substitutions: list[str]
    ingredient_id: str
    nutrition_source: str


# Nhóm nguyên liệu, ví dụ: bumbu, nước dùng, topping.
class IngredientGroup(SchemaBaseModel):
    group_name: str
    items: list[Ingredient]


# Nhiệt độ có thể xuất hiện trong bước nấu.
class Temperature(SchemaBaseModel):
    celsius: float
    fahrenheit: float


# Dấu hiệu nhận biết độ chín/đúng trạng thái của món ăn.
class DonenessCues(SchemaBaseModel):
    visual: str | None
    tactile: str | None


# Dữ liệu có cấu trúc để agent hiểu hành động, thời gian và dấu hiệu.
class StructuredInstruction(SchemaBaseModel):
    action: str
    temperature: Temperature | None
    duration: str
    doneness_cues: DonenessCues | None


# Một bước trong quy trình nấu ăn.
class Instruction(SchemaBaseModel):
    step_number: int
    phase: str
    text: str
    structured: StructuredInstruction
    tips: list[str]


# Cách xử lý lỗi thường gặp khi nấu công thức này.
class Troubleshooting(SchemaBaseModel):
    symptom: str
    likely_cause: str
    prevention: str
    fix: str


# Giá trị dinh dưỡng chi tiết tính trên mỗi khẩu phần.
class NutritionalInfo(SchemaBaseModel):
    calories: float
    protein_g: float
    carbs_g: float = Field(alias="carbohydrates_g")
    fat_g: float
    saturated_fat_g: float
    trans_fat_g: float
    monounsaturated_fat_g: float
    polyunsaturated_fat_g: float
    fiber_g: float
    sugar_g: float
    sodium_mg: float
    cholesterol_mg: float
    potassium_mg: float
    calcium_mg: float
    iron_mg: float
    magnesium_mg: float
    phosphorus_mg: float
    zinc_mg: float
    vitamin_a_mcg: float
    vitamin_c_mg: float
    vitamin_d_mcg: float
    vitamin_e_mg: float
    vitamin_k_mcg: float
    vitamin_b6_mg: float
    vitamin_b12_mcg: float
    thiamin_mg: float
    riboflavin_mg: float
    niacin_mg: float
    folate_mcg: float
    water_g: float
    alcohol_g: float | None
    caffeine_mg: float | None


# Khối dinh dưỡng gồm dinh dưỡng mỗi khẩu phần và nguồn dữ liệu.
class Nutrition(SchemaBaseModel):
    per_serving: NutritionalInfo
    sources: list[str]


# Nội dung chính của công thức trong trường "data" của JSON.
class Recipe(SchemaBaseModel):
    # Mã định danh duy nhất của công thức.
    id: str
    # Tên món ăn hiển thị cho người dùng.
    name: str
    # Mô tả ngắn về hương vị, thành phần chính và cách phục vụ.
    description: str
    # Nhóm món ăn chính, ví dụ: Soup, Main Course.
    category: str
    # Nền ẩm thực hoặc quốc gia/vùng miền của món ăn.
    cuisine: str
    # Mức độ khó khi thực hiện công thức.
    difficulty: str
    # Các từ khóa dùng để tìm kiếm, lọc hoặc gợi ý món ăn.
    tags: list[str]
    # Thông tin thời gian nấu, số khẩu phần và khối lượng mỗi phần.
    meta: Meta
    # Thông tin ăn kiêng và các đối tượng không phù hợp.
    dietary: Dietary
    # Hướng dẫn bảo quản, cấp đông và hâm nóng món ăn.
    storage: Storage
    # Danh sách dụng cụ cần chuẩn bị trước khi nấu.
    equipment: list[Equipment]
    # Danh sách nguyên liệu được chia theo từng nhóm sử dụng.
    ingredients: list[IngredientGroup]
    # Các bước nấu ăn theo thứ tự, kèm dữ liệu có cấu trúc cho agent.
    instructions: list[Instruction]
    # Các lỗi thường gặp, nguyên nhân, cách phòng tránh và cách khắc phục.
    troubleshooting: list[Troubleshooting]
    # Ghi chú thêm từ đầu bếp để cải thiện hương vị hoặc thao tác.
    chef_notes: list[str]
    # Bối cảnh văn hóa hoặc thông tin nền về món ăn.
    cultural_context: str
    # Thông tin dinh dưỡng chi tiết và nguồn dữ liệu dinh dưỡng.
    nutrition: Nutrition


# Thông tin hạn mức sử dụng API trả về kèm công thức.
class Usage(SchemaBaseModel):
    monthly_remaining: int
    monthly_limit: int
    daily_remaining: int
    daily_limit: int
    plan_key: str
    plan_name: str
    period_type: str
    detail_used: int
    detail_limit: int
    detail_remaining: int
    generation_used: int
    generation_limit: int
    generation_remaining: int
    generation_overage_remaining: int
    reset_at: str | None
    upgrade_url: str


# Schema cấp cao nhất khớp với file JSON mẫu: gồm data và usage.
class RecipeResponse(SchemaBaseModel):
    data: Recipe
    usage: Usage


__all__ = [
    "Dietary",
    "DonenessCues",
    "Equipment",
    "Ingredient",
    "IngredientGroup",
    "Instruction",
    "Meta",
    "Nutrition",
    "NutritionalInfo",
    "Recipe",
    "RecipeResponse",
    "Storage",
    "StorageMethod",
    "StructuredInstruction",
    "Temperature",
    "Troubleshooting",
    "Usage",
]
