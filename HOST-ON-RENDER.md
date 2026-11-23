# Put Wakilni online for your professor

The online copy runs on Render, so your professor can open it even when your computer is off. The upload ZIP contains the latest illustrated website and the synthetic starter database. Your own local copy can stay on your computer.

## 1. Upload the website to GitHub

1. Extract **Wakilni Website - Render Upload.zip** in File Explorer.
2. Sign in to [GitHub](https://github.com/) and [create a private repository](https://github.com/new), for example `wakilni-class-demo`. Select **Add a README file** to initialize it.
3. In your repository, click **Add file → Upload files**. Open the extracted upload folder and drag in its contents, including the `static` and `templates` folders. Upload the contents rather than the ZIP itself. Choose **Commit changes**.
4. Check that `render.yaml`, `app.py`, `requirements.txt`, `requirements-render.txt`, `wakilni_demo.sqlite`, `static`, and `templates` appear at the top level of the repository.

[GitHub upload instructions](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)

## 2. Deploy the free site on Render

1. Sign in to the [Render dashboard](https://dashboard.render.com/). Choose **New → Blueprint**.
2. Connect your GitHub account if prompted. Give Render access to your demo repository and choose **Connect** next to it.
3. Give the Blueprint a name, such as **Wakilni class demo**. Keep `render.yaml` as the path. The file sets up a **Free** web service and uses the included SQLite file.
4. When Render asks for `WAKILNI_DEMO_PASSWORD`, choose a new password of at least 12 characters. Keep it in Render and share it with your professor. The site’s username is **`professor`**.
5. Click **Deploy Blueprint**. Once the web service is live, open it in Render and copy the HTTPS address ending in **`.onrender.com`**.

[Render Blueprint setup](https://render.com/docs/infrastructure-as-code)

## 3. Check the link before sharing it

Open the address in a private browser window. The browser should ask for a username and password. Sign in with **`professor`** and your demo password. Search for `WK0000000042` to confirm that the database and website are connected. Then give your professor the HTTPS link and demo login. She can use her regular browser.

Render Free can fall asleep after 15 minutes of inactivity. The next visitor may wait about a minute for it to wake up. The included database starts with the original demo data. Orders and shipment changes made online can disappear when Render goes to sleep, restarts, or redeploys. Use the local website for changes you want to keep. The deployment only includes a Free web service. Stay on the Free plan and leave billing details off the account if you want Render to suspend service at the free usage limits instead of billing for overages.

[Render Free plan details](https://render.com/docs/free)
