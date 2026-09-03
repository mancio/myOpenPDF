from app.schemas import ScanParams

BUILTIN_PRESETS: dict[str, ScanParams] = {
    "subtle": ScanParams(dpi=300, color_mode="gray", contrast=1.05, noise_sigma=4, blur_sigma=0.2, jpeg_quality=90),
    "medium": ScanParams(
        dpi=200,
        color_mode="gray",
        contrast=1.18,
        noise_sigma=10,
        blur_sigma=0.4,
        jpeg_quality=75,
    ),
    "heavy": ScanParams(
        dpi=150,
        color_mode="gray",
        contrast=1.35,
        noise_sigma=20,
        blur_sigma=0.7,
        jpeg_quality=55,
    ),
    "photocopy": ScanParams(dpi=200, color_mode="bw", contrast=1.6, noise_sigma=12),
    "fax": ScanParams(dpi=100, color_mode="bw", contrast=1.8, blur_sigma=0.3, noise_sigma=6, bw_dither=False),
    "archive": ScanParams(dpi=200, color_mode="color", paper_tint="#F7F0DF", noise_sigma=6, jpeg_quality=65),
}
