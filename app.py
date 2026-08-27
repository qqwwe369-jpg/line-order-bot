from flask import Flask, request

app = Flask(__name__)


@app.route("/", methods=["GET"])
def home():
    return "LINE Order Bot is running!", 200


@app.route("/callback", methods=["POST"])
def callback():
    print("Webhook received")
    print(request.get_data(as_text=True))

    return "OK", 200


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
