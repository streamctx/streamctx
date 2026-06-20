from setuptools import find_packages, setup

with open("README.md", encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="streamctx",
    version="0.3.1",
    description="Context health monitoring for AI agents — detect poisoning, drift, loops",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Sneh R Joshi",
    author_email="joshisneh51@gmail.com",
    url="https://github.com/streamctx/streamctx",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    install_requires=[
        "rich>=13.0.0",
    ],
    extras_require={
        "openai": ["openai>=1.0.0"],
        "anthropic": ["anthropic>=0.25.0"],
        "all": ["openai>=1.0.0", "anthropic>=0.25.0"],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Software Development :: Libraries",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    keywords=[
        "llm", "ai", "agent", "context", "monitoring",
        "observability", "openai", "anthropic", "token",
        "checkpoint", "compression", "self-healing",
    ],
)
