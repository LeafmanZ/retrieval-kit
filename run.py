from retrieval_kit import create_standalone_app

app = create_standalone_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
