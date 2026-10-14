"""
Sky-quality physics: airmass, extinction, sky brightness and the moon's contribution to it.

Everything is in V band, the band the published models and SQM readings use.

References
- Kasten & Young (1989), Applied Optics 28, 4735: airmass formula valid to the horizon.
- Krisciunas & Schaefer (1991), PASP 103, 1033 ("K&S"): brightness of moonlit sky.
- Garstang (1989) / K&S eq. 2: dark-sky brightness rising towards the horizon.
- Hayes & Latham (1975): Rayleigh extinction scale with altitude; aerosol term from AOD.
"""
import numpy as np

NATURAL_SKY = 22.0  # mag/arcsec², a pristine moonless zenith sky (the light-pollution atlas uses this too)

# Bortle class from total zenith brightness (lower SQM bound of each class), the common SQM↔Bortle table.
_BORTLE_SQM = [(21.99, 1), (21.89, 2), (21.69, 3), (20.49, 4), (19.50, 5), (18.94, 6), (18.38, 7), (17.80, 8)]
# Representative zenith SQM for a Bortle class someone sets by hand.
BORTLE_TO_SQM = {1: 21.95, 2: 21.9, 3: 21.8, 4: 21.1, 5: 20.0, 6: 19.2, 7: 18.7, 8: 18.1, 9: 17.5}


def bortle_from_sqm(sqm):
    for floor, cls in _BORTLE_SQM:
        if sqm >= floor:
            return cls
    return 9


def airmass(alt_deg):
    """Kasten & Young (1989) relative airmass; altitude in degrees, NaN below the horizon."""
    alt = np.asarray(alt_deg, dtype=float)
    z = 90.0 - alt
    with np.errstate(invalid="ignore", divide="ignore"):
        x = 1.0 / (np.cos(np.radians(z)) + 0.50572 * (96.07995 - z) ** -1.6364)
    return np.where(alt > 0, np.maximum(x, 1.0), np.nan)  # the fit gives 0.9997 at the zenith


def _ks_airmass(z_deg):
    """The airmass form K&S use inside their model (eq. 3)."""
    return (1.0 - 0.96 * np.sin(np.radians(z_deg)) ** 2) ** -0.5


def extinction_coefficient(elevation_m, aod=None):
    """
    V-band extinction k (mag per airmass) = Rayleigh + ozone + aerosol.
    Rayleigh falls with the air above you (scale height 8 km); aerosol optical depth τ
    converts to 1.086 τ mag. Without an AOD forecast use τ = 0.1, a typical clean-air value.
    """
    rayleigh = 0.1451 * np.exp(-max(float(elevation_m), 0.0) / 7996.0)
    ozone = 0.016
    tau = 0.1 if aod is None else np.asarray(aod, dtype=float)
    return rayleigh + ozone + 1.086 * tau


def _mag_to_nl(mag):
    """Surface brightness mag/arcsec² → nanoLamberts (K&S eq. 1 inverse)."""
    return 34.08 * np.exp(20.7233 - 0.92104 * np.asarray(mag, dtype=float))


def dark_sky_nl(zenith_sqm, alt_deg, k):
    """
    Moonless sky brightness at altitude `alt_deg`, nL. The natural part brightens towards the
    horizon (K&S eq. 2, van Rhijn); the artificial part is taken to scale the same way.
    """
    z = 90.0 - np.asarray(alt_deg, dtype=float)
    x = _ks_airmass(np.clip(z, 0, 90))
    return _mag_to_nl(zenith_sqm) * 10 ** (-0.4 * k * (x - 1)) * x


def moon_sky_nl(moon_alt, phase_angle_deg, separation_deg, target_alt, k):
    """
    Krisciunas & Schaefer (1991) brightness of moonlight scattered at the target position, nL.
    phase_angle 0 = full moon, 180 = new. Zero when the moon is down.
    """
    alpha = np.abs(np.asarray(phase_angle_deg, dtype=float))
    # Below ~10° from the moon the scattering fit isn't valid; clamp there (you won't image that close anyway).
    rho = np.clip(np.asarray(separation_deg, dtype=float), 10.0, 180.0)
    illum = 10 ** (-0.4 * (3.84 + 0.026 * alpha + 4e-9 * alpha ** 4))  # eq. 20, moon brightness
    f_rho = 10 ** 5.36 * (1.06 + np.cos(np.radians(rho)) ** 2) + 10 ** (6.15 - rho / 40.0)  # eq. 21
    zm = 90.0 - np.asarray(moon_alt, dtype=float)
    z = 90.0 - np.clip(np.asarray(target_alt, dtype=float), 0, 90)
    b = f_rho * illum * 10 ** (-0.4 * k * _ks_airmass(np.clip(zm, 0, 90))) * (1 - 10 ** (-0.4 * k * _ks_airmass(z)))
    return np.where(np.asarray(moon_alt) > 0, b, 0.0)


def moon_brightening_mag(zenith_sqm, moon_alt, phase_angle_deg, separation_deg, target_alt, k):
    """How many magnitudes the moon brightens the sky at the target (0 = no effect)."""
    dark = dark_sky_nl(zenith_sqm, target_alt, k)
    moon = moon_sky_nl(moon_alt, phase_angle_deg, separation_deg, target_alt, k)
    return 2.5 * np.log10((dark + moon) / dark)


def deep_sky_components(zenith_sqm, target_alt, moon_alt, phase_angle_deg, separation_deg, k, cloud_pct):
    """
    Value of one unit of time relative to the same time at this site's zenith on a clear,
    moonless night. For sky-limited imaging SNR² ∝ t·S²/B, so equal SNR needs
    t_eff = t · (S/S_zenith)² · (B_zenith,dark / B). Returned as three factors whose product
    is that weight, so a night can say where its hours went:
      - altitude: (S/S_zenith)² · B_zenith/B_dark(alt); S/S_zenith = 10^(-0.4 k (X-1)) is light
        lost to the extra air, and the dark sky itself brightens towards the horizon
      - moon:     B_dark / (B_dark + B_moon), moonlight from K&S
      - clear:    expected clear fraction, 1 - cloud cover
    """
    x = airmass(target_alt)
    signal2 = 10 ** (-0.8 * k * (x - 1))
    b_ref = _mag_to_nl(zenith_sqm)
    b_dark = dark_sky_nl(zenith_sqm, target_alt, k)
    b_moon = moon_sky_nl(moon_alt, phase_angle_deg, separation_deg, target_alt, k)
    altitude = np.nan_to_num(signal2 * b_ref / b_dark, nan=0.0)
    moon = b_dark / (b_dark + b_moon)
    clear = 1 - np.clip(np.nan_to_num(np.asarray(cloud_pct, dtype=float), nan=100.0), 0, 100) / 100
    return altitude, moon, clear


def deep_sky_weight(*args):
    altitude, moon, clear = deep_sky_components(*args)
    return altitude * moon * clear


def seeing_factor(jet_kmh):
    """
    Heuristic seeing quality 0.25–1 from 250 hPa (jet-stream) wind. Fast upper-level wind is the
    best single forecast predictor of bad seeing: ≤ 40 km/h is calm, ~100 km/h noticeably soft,
    ≥ 160 km/h poor. A proxy, not a measurement — real seeing also depends on local turbulence.
    """
    v = np.nan_to_num(np.asarray(jet_kmh, dtype=float), nan=80.0)
    return np.clip(1.25 - v / 160.0, 0.25, 1.0)


def seeing_label(jet_kmh):
    if jet_kmh is None:
        return None
    return "good" if jet_kmh < 60 else "average" if jet_kmh < 110 else "poor"


def planetary_weight(target_alt, k, cloud_pct, jet_kmh):
    """
    For planets and the moon (bright, seeing-limited): resolution matters, not sky darkness.
    Seeing FWHM grows as X^0.6, so resolution-equivalent time scales as X^-1.2; times the
    extinction loss, the clear fraction and the jet-stream seeing factor.
    """
    x = airmass(target_alt)
    clear = 1 - np.clip(np.nan_to_num(np.asarray(cloud_pct, dtype=float), nan=100.0), 0, 100) / 100
    w = 10 ** (-0.8 * k * (x - 1)) * x ** -1.2 * clear * seeing_factor(jet_kmh)
    return np.nan_to_num(w, nan=0.0)


def angular_separation(alt1, az1, alt2, az2):
    """Great-circle distance between two alt/az positions, degrees."""
    a1, a2 = np.radians(alt1), np.radians(alt2)
    daz = np.radians(np.asarray(az1) - np.asarray(az2))
    c = np.sin(a1) * np.sin(a2) + np.cos(a1) * np.cos(a2) * np.cos(daz)
    return np.degrees(np.arccos(np.clip(c, -1, 1)))
