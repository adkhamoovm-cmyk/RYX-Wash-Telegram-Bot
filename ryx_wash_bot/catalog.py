from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CarModel:
    id: str
    category: str
    name: str
    price: int


# Initial service catalog. Add or change entries here as the RYX Wash price list
# changes; the order stores a snapshot of the selected name and price.
CAR_MODELS: tuple[CarModel, ...] = (
    CarModel("sedan-cobalt", "Sedan", "Chevrolet Cobalt", 50_000),
    CarModel("sedan-nexia3", "Sedan", "Chevrolet Nexia 3", 50_000),
    CarModel("sedan-malibu", "Sedan", "Chevrolet Malibu", 70_000),
    CarModel("hatchback-spark", "Hatchback", "Chevrolet Spark", 40_000),
    CarModel("hatchback-matiz", "Hatchback", "Daewoo Matiz", 40_000),
    CarModel("suv-tracker", "SUV", "Chevrolet Tracker", 70_000),
    CarModel("suv-captiva", "SUV", "Chevrolet Captiva", 80_000),
    CarModel("suv-equinox", "SUV", "Chevrolet Equinox", 80_000),
    CarModel("minivan-damas", "Minivan", "Chevrolet Damas", 50_000),
)


def categories() -> list[str]:
    return list(dict.fromkeys(model.category for model in CAR_MODELS))


def models_for_category(category: str) -> list[CarModel]:
    return [model for model in CAR_MODELS if model.category == category]


def get_model(model_id: str) -> CarModel | None:
    return next((model for model in CAR_MODELS if model.id == model_id), None)


def get_model_by_name(category: str, name: str) -> CarModel | None:
    return next(
        (
            model
            for model in CAR_MODELS
            if model.category == category and model.name == name
        ),
        None,
    )


def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " so'm"
