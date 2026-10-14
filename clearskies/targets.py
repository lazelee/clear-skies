import re

# Offline catalog of popular astrophotography targets (J2000 RA/Dec in degrees).
# Kept local so planning works without a network name resolver.
TARGETS = [
    ("none", "No target (wide-field / sky only)", "Any", None, None),
    ("M31", "M31 Andromeda Galaxy", "Galaxy", 10.6847, 41.2690),
    ("M33", "M33 Triangulum Galaxy", "Galaxy", 23.4621, 30.6599),
    ("M51", "M51 Whirlpool Galaxy", "Galaxy", 202.4696, 47.1952),
    ("M81", "M81 Bode's Galaxy", "Galaxy", 148.8882, 69.0653),
    ("M82", "M82 Cigar Galaxy", "Galaxy", 148.9685, 69.6797),
    ("M101", "M101 Pinwheel Galaxy", "Galaxy", 210.8024, 54.3488),
    ("M104", "M104 Sombrero Galaxy", "Galaxy", 189.9976, -11.6231),
    ("M42", "M42 Orion Nebula", "Emission nebula", 83.8221, -5.3911),
    ("B33", "Horsehead Nebula", "Emission nebula", 85.2458, -2.4583),
    ("NGC2237", "Rosette Nebula", "Emission nebula", 97.9792, 4.9417),
    ("NGC7000", "North America Nebula", "Emission nebula", 314.6833, 44.3333),
    ("IC1396", "Elephant's Trunk (IC 1396)", "Emission nebula", 324.7417, 57.5000),
    ("IC1805", "Heart Nebula", "Emission nebula", 38.1750, 61.4500),
    ("IC1848", "Soul Nebula", "Emission nebula", 42.7875, 60.4333),
    ("M8", "M8 Lagoon Nebula", "Emission nebula", 270.9042, -24.3867),
    ("M16", "M16 Eagle Nebula", "Emission nebula", 274.7000, -13.8067),
    ("M17", "M17 Omega Nebula", "Emission nebula", 275.1958, -16.1717),
    ("M20", "M20 Trifid Nebula", "Emission nebula", 270.6208, -22.9717),
    ("NGC6960", "Veil Nebula (West)", "Supernova remnant", 311.4292, 30.7167),
    ("M1", "M1 Crab Nebula", "Supernova remnant", 83.6331, 22.0145),
    ("M27", "M27 Dumbbell Nebula", "Planetary nebula", 299.9016, 22.7211),
    ("M57", "M57 Ring Nebula", "Planetary nebula", 283.3963, 33.0292),
    ("NGC7293", "Helix Nebula", "Planetary nebula", 337.4108, -20.8372),
    ("M45", "M45 Pleiades", "Star cluster", 56.7500, 24.1167),
    ("M13", "M13 Hercules Cluster", "Star cluster", 250.4235, 36.4613),
    ("NGC869", "Double Cluster", "Star cluster", 35.0833, 57.1333),
    ("M44", "M44 Beehive Cluster", "Star cluster", 130.1000, 19.6667),
    ("GC", "Milky Way core", "Milky Way", 266.4168, -29.0078),
    ("NGC253", "Sculptor Galaxy", "Galaxy", 11.8880, -25.2883),
    ("LMC", "Large Magellanic Cloud", "Galaxy", 80.8942, -69.7561),
]

def _short(tid, name):
    # Messier objects go by their number; everything else by its common name.
    return tid if re.match(r"^M\d+$", tid) else name


BY_ID = {t[0]: {"id": t[0], "name": t[1], "short": _short(t[0], t[1]), "type": t[2], "ra": t[3], "dec": t[4]} for t in TARGETS}


def as_list():
    return [BY_ID[t[0]] for t in TARGETS]


def get(target_id):
    return BY_ID.get(target_id or "none", BY_ID["none"])
