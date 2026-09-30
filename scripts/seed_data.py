"""The sample catalog that the seed script inserts (spec section 5.6).

The data is made up. Every value passes the same schema that the API applies to an admin's input,
and a unit test checks that.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SeedTest:
    code: str
    name: str
    description: str


@dataclass(frozen=True, slots=True)
class SeedCentre:
    name: str
    location: str
    city: str
    prices: Mapping[str, int]  # test code -> price in whole rupees (INR)


TESTS = (
    SeedTest("CBC", "Complete Blood Count", "Counts red cells, white cells and platelets."),
    SeedTest("LIPID", "Lipid Profile", "Total cholesterol, HDL, LDL and triglycerides."),
    SeedTest("HBA1C", "HbA1c", "Average blood sugar over the last three months."),
    SeedTest("FBS", "Fasting Blood Sugar", "Blood sugar after eight hours without food."),
    SeedTest("TSH", "Thyroid Stimulating Hormone", "Shows how well the thyroid gland works."),
    SeedTest("LFT", "Liver Function Test", "Enzymes and proteins that show how the liver works."),
    SeedTest("KFT", "Kidney Function Test", "Creatinine, urea and uric acid in the blood."),
    SeedTest("VITD", "Vitamin D (25-Hydroxy)", "The level of vitamin D in the blood."),
    SeedTest("VITB12", "Vitamin B12", "Finds a lack of vitamin B12."),
    SeedTest("URINE", "Urine Routine", "A physical, chemical and microscopic urine check."),
    SeedTest("ECG", "Electrocardiogram", "Records the electrical activity of the heart."),
    SeedTest("XRAY_CHEST", "Chest X-Ray", "An image of the lungs, the heart and the chest wall."),
)

CENTRES = (
    SeedCentre(
        "Sunrise Diagnostics Koramangala",
        "80 Feet Road, Koramangala 4th Block",
        "Bengaluru",
        {"CBC": 350, "LIPID": 650, "HBA1C": 550, "TSH": 450, "VITD": 1200, "URINE": 150},
    ),
    SeedCentre(
        "Sunrise Diagnostics Indiranagar",
        "100 Feet Road, Indiranagar",
        "Bengaluru",
        {"CBC": 380, "FBS": 100, "LFT": 700, "KFT": 800, "VITB12": 900, "ECG": 400},
    ),
    SeedCentre(
        "City Care Labs Bandra",
        "Linking Road, Bandra West",
        "Mumbai",
        {"CBC": 450, "LIPID": 800, "HBA1C": 600, "TSH": 500, "XRAY_CHEST": 500, "ECG": 550},
    ),
    SeedCentre(
        "City Care Labs Connaught Place",
        "Block F, Connaught Place",
        "New Delhi",
        {"FBS": 120, "LFT": 750, "KFT": 850, "VITD": 1400, "VITB12": 1000, "URINE": 180},
    ),
    SeedCentre(
        "Lifeline Diagnostics Banjara Hills",
        "Road No. 12, Banjara Hills",
        "Hyderabad",
        {"CBC": 320, "LIPID": 600, "FBS": 90, "TSH": 420, "XRAY_CHEST": 450, "URINE": 130},
    ),
)
