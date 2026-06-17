import reflex as rx

config = rx.Config(
    app_name="lca_web",
    plugins=[
        rx.plugins.SitemapPlugin(),
        rx.plugins.TailwindV4Plugin(),
    ]
)