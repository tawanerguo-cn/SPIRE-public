from io import BytesIO
import numpy as np


def spectrum_channel_quality(spectrum) -> dict:
    quality = {}
    for name, row in zip(spectrum.channel_names, spectrum.data):
        values = np.asarray(row)
        finite = np.isfinite(values)
        quality[name] = {"finite_count": int(finite.sum()), "point_count": len(values),
                         "all_zero": bool(len(values) and finite.all() and (values == 0).all()),
                         "constant": bool(len(values) and finite.all() and np.ptp(values) == 0)}
    return quality


def render_spectrum_arrays(x, y, *, channel: str, axis_channel: str) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    figure = Figure(figsize=(7, 4), layout="constrained")
    FigureCanvasAgg(figure)
    axis = figure.subplots()
    axis.plot(x, y, linewidth=1, marker=".", markersize=3)
    axis.set(xlabel=axis_channel, ylabel=channel, title="Saved spectrum — identity requires reviewed references")
    axis.grid(alpha=0.2)
    output = BytesIO()
    figure.savefig(output, format="png", dpi=150)
    return output.getvalue()
