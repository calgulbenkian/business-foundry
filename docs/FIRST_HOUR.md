# Your first hour with Foundry

This guide is for someone starting or buying a business who rarely uses a terminal. It takes you from nothing installed to your own business workspace.

## What Foundry is, and what it is not

Foundry is a private workspace on your own computer for the work of starting, buying and running a business: customers, offers, decisions, evidence, money and the steps still to do. It helps you prepare and track that work, and it asks a person to review each important step. It does not file with governments, open bank accounts, sign agreements or move money; you do those with the relevant people and providers, then record the receipts in Foundry. It does not certify legal compliance or replace legal, tax, accounting or investment judgment.

This guide was checked on macOS. The project's automated checks run on Ubuntu Linux. Windows and other systems have not been tried.

## Two ways to open Foundry

This guide uses `foundry start`, which runs Foundry from a terminal and shows it in your web browser. There is also a desktop window, `foundry-desktop`, described in the [README](../README.md#desktop-window-preview). It is a preview.

The two keep **different databases**. `foundry start` keeps its records in `.foundry/foundry.db` inside the folder you start it from, or in the file you name with `--db`. The desktop window keeps its records in your per-user data folder (on a Mac, `~/Library/Application Support/Business Foundry`). So a business you make in one is not in the other. The desktop window's first run starts with an empty workspace and does not add the example business.

Use `foundry start` to work in your web browser, or when an AI client needs to reach Foundry at a fixed address. Use the desktop window if you would rather have Foundry in a window of its own, with no key to paste. Pick one for a business and keep to it.

## What you need

- **A terminal.** This is a window where you type commands and press Enter. On a Mac, open the **Terminal** app (in Applications, then Utilities). On Linux, look for Terminal among your applications.
- **uv**, a free tool that installs and runs Python programs. Follow [uv's installation page](https://docs.astral.sh/uv/getting-started/installation/). If the terminal later says it cannot find `uv`, close the terminal window and open a new one. You do not need to install Python yourself: if your computer lacks Python 3.11 or newer, uv downloads it.
- **An internet connection for the first start**, when uv downloads what Foundry needs. After that, Foundry runs without one.
- **The Foundry code.** Choose one way:
  - With git, type `git clone https://github.com/calgulbenkian/business-foundry.git` in the terminal.
  - Without git, open https://github.com/calgulbenkian/business-foundry in your web browser, choose **Code**, then **Download ZIP**, and unzip the file.

## Start Foundry and sign in

1. In the terminal, move into the Foundry folder with `cd` ("change directory") and the folder's name. For a git copy that is `cd business-foundry`. For a ZIP copy, type `cd ` (with a space) and drag the unzipped folder onto the terminal window, then press Enter.
2. Type `uv run foundry start` and press Enter.

The first time, uv prints a few lines while it sets up (for example "Creating virtual environment" and "Installed … packages"). Then Foundry prints:

```text
Created a new private workspace. Its sign-in key is saved in …/business-foundry/.foundry/owner.key
Added the example business 'Polymer Geospatial · demo'. It is synthetic: its customers, figures and receipts are made up.

Business Foundry is running.

  Open this address in your web browser: http://127.0.0.1:8765
  Sign-in key: bf_…

Keep the key private. Anyone who has it can sign in as the owner of this workspace.
To stop Foundry, press Ctrl+C in this window.
```

If you press Ctrl+C while Foundry is still setting up, it finishes setting up first and then stops, so the example is never left half made. Run `uv run foundry start` again to open it.

Your web browser then opens at **http://127.0.0.1:8765**. If it does not, open your browser and type that address yourself. `127.0.0.1` means "this computer": only programs on your own computer can reach this address.

The page says **Open your workspace**. Copy the whole sign-in key from the terminal (it starts with `bf_`), paste it into **Access key**, and choose **Open workspace**. If you lose track of the key, **Finding your key** on that page says where it is. Leave the terminal window open while you work; Foundry runs inside it.

If the terminal instead says the port is already in use, Foundry may already be running in another terminal window. Open http://127.0.0.1:8765 in your browser, or start on another port with `uv run foundry start --port 8766`.

## Ten minutes in the example business

You start in **Polymer Geospatial · demo**. The **Synthetic demo** tag in the toolbar is a reminder that its customers, figures and receipts are invented. Try anything; unless you set up the optional AI features, nothing in Foundry sends your records anywhere.

The page has three panes side by side. On a phone they stack: you see one at a time, and a back button at the top returns to the one before.

- **On the left, the sources.** **Journey** and **Today** come first. Under **Lines** is one entry for each group of steps, such as **Market**, **Setup** or **Sales to cash**, with how many of its steps are verified. Under **Records** are **Evidence**, **Business records**, **Money**, **Acquisition analyses**, **Documents** and **Invoices & payments**, then **History** and **Access & assistants**. At the bottom, **Key** explains the mark used for each state of a step.
- **In the middle, the list** for the source you chose, grouped under headings.
- **On the right, the inspector.** It shows the item you chose in the list, with its buttons.

The toolbar along the top holds **Choose business**, which switches between businesses, a search box for the current list, **+ New business** and **Sign out**.

What each source shows:

- **Today**: a count of steps that are verified, with an outside body, waiting on you and not started, then the steps that need you, grouped as **Needs review**, **Waiting on you**, **Waiting on someone else** and **Ready to start**. Below them are the money figures Foundry calculated.
- **Journey**: a map of every step. Each line is a colour, its stops are its steps, and every line runs to the same stop, **Open for business**. Choose a stop on the map, or in the list of stops, to see it in the inspector. The inspector also has **Edit business**.
- **A line**, such as **Setup**: that line's steps in order. Choose a step to see what it is, who acts, its **Stages** (**Prepared**, **Approved**, **Submitted**, **Verified**) and its history. Its buttons record your decision, for example **Start work…**, **Mark prepared…**, **Approve…** or **Not applicable…**. Each opens a short form with **Next state**, **Evidence for this decision** and **Decision note**; **Record decision** saves it.
- **Evidence**: the original text behind the records and decisions. **+ Add evidence** adds an item. An owner or reviewer can **Record human review**.
- **Business records**: what you know or assume about customers, offers, assets, contracts, roles and more, each with its evidence status, for example **Reported**. **+ Add record** adds one.
- **Money**: the financial model, with monthly income, cash and balance sheet under base, downside and upside cases. **+ New version** saves a revised model.
- **Acquisition analyses**: an analysis of buying a business. **+ Analyze a business** starts one.
- **Documents**: drafts such as a business plan, written from your records. **Read draft** opens one; **+ Generate document** makes a new one.
- **Invoices & payments**: **+ Draft invoice**, **Record issue** and **Record payment** record what you did with your invoicing provider and bank. Foundry does not send invoices.
- **History**: every change, who made it and when, and an **Integrity check** that history and current records agree. **Export business records** downloads a copy.
- **Access & assistants**: access keys for other people (**+ Create access key**), **Backup and restore**, and the optional assistant review, which works only after it is set up (see [AI setup](AI.md)).

## Create your own business

Choose **+ New business** in the toolbar. (If you started with `--empty`, the page also says **Start your first business** and offers **Create a business**, which opens the same form.) The form, **Create a business**, asks for:

- **Business name**: what you call it for now.
- **Entry path**: **Start a business** or **Acquire a business**.
- **Concept**: at least ten characters. Describe the buyer, their problem, what you will offer and why it could work.
- **Intended customer**: who you expect to buy.
- **Accountable owner**: the person responsible for the work. It is a name on the records, not a sign-in.
- **Planning jurisdiction**: the country or state whose planning guide to use. Choose **Undecided — review needed** until someone qualified has looked at where you will operate. It picks a planning guide, not a legal entity or tax choice. See below for changing it later.
- **Model currency**: a three-letter code such as USD, EUR or GBP. The financial model uses this one currency.
- **This workspace contains synthetic demonstration data.**: leave this unticked for a real business.

**Create workspace** makes the business and its steps, and opens it on **Today**. Unknowns can stay unknown until you have evidence.

### Changing the planning jurisdiction later

You can change it later, but not in the page: **Edit business** cannot change it, and no other control does. An owner or reviewer changes it as a recorded decision with a reason. As the workspace owner, you can do it from the terminal, in the Foundry folder:

```sh
uv run foundry jurisdiction BUSINESS_ID GB --preview
uv run foundry jurisdiction BUSINESS_ID GB --reason "Why the founders chose it."
```

The first line shows what would change and changes nothing. The second makes the change. Replace `BUSINESS_ID` with the business's ID and `GB` with the code you want (`US-DE`, `GB`, `SE`, `other` or `undecided`). To find the ID, choose **Export business records** under **History**. In the file it saves, the business's `id` is under `"business"` and starts with `business_`. The steps of the old guide stay in the workspace as history, the new guide's steps are added, and completed work reopens for review. The [walkthrough](WALKTHROUGH.md) explains what else changes. A reviewer, or a program with an owner or reviewer key, makes the change through the API with `POST /api/businesses/{id}/jurisdiction`.

## What the words on screen mean

Records have an **evidence status**:

- **Assumption**: something you believe but have not checked.
- **Reported**: something a person or a document states.
- **Verified**: an owner or reviewer has checked it against evidence that was itself reviewed.

Steps move through states. The **Key** at the bottom of the sources lists them:

- **Not started** or **In progress**: not ready for a decision yet.
- **Prepared**: the work is done and ready for a person to decide on.
- **Approved**: an owner or reviewer approved it, with reviewed evidence, after its prerequisites were met.
- **Submitted**: for steps done outside Foundry, such as a filing: you sent it, and a reviewed receipt is attached.
- **Verified**: the result is on record and a person checked it.
- **Not applicable**: a reviewer decided, with evidence, that the step does not apply.
- **Needs review**: something it relied on changed, so it reopened.

These states are kept apart on purpose: preparing something is not approving it, and sending something is not the same as confirming it worked.

**Evidence** is the original text you paste in, such as an email, an agreement clause or interview notes, with an optional web address. It cannot be edited after saving; a correction is a new item. It shows **Not yet reviewed** until an owner or reviewer records a human review, and then **Reviewed**. If that review is withdrawn with **Withdraw verification**, the item is kept and marked **Withdrawn**, and completed work that relied on it reopens as **Needs review**. An **external receipt** is evidence that something happened outside Foundry, such as a filing confirmation or a bank receipt.

## Stop, start again, and keep your data safe

- **To stop**, press Ctrl+C in the terminal window. Foundry says it has stopped. Closing the terminal window also stops it.
- **To start again**, open a terminal, `cd` into the same Foundry folder and run `uv run foundry start`. It shows the same key and does not add the example again. Always start from the same folder: Foundry keeps its data in the folder you start it from.
- **Where your data is**: in the `.foundry` folder inside the Foundry folder. `foundry.db` holds your records and `owner.key` holds your sign-in key. Folders whose names start with a dot are hidden; on a Mac, press Command+Shift+. (full stop) in Finder to show them. The desktop window's records are elsewhere, as described above.
- **To back up**, open a second terminal window, `cd` into the Foundry folder and run `uv run foundry backup ~/foundry-backups/foundry-1.db`. Foundry can do this while it is running. It never overwrites a file, so choose a new name each time. The backup holds every business in the workspace and is not encrypted; keep it somewhere private.
- **The sign-in key** is a long secret that lets anyone who has it act as the owner of every business in this workspace. Foundry keeps only a fingerprint of it, so the backup does not contain the key itself. Copy `owner.key` to the same private place as your backups. `uv run foundry token` prints the key again.
- **If `owner.key` is lost**, the copy from the point above is what lets you sign in again, so keep one in a private place. Without a copy, an owner key can be replaced only while you are still signed in as an owner somewhere. A sign-in in the browser lasts 8 hours, or until you sign out. The page has no control for making an owner key yet. Once no owner key and no owner sign-in is left, you cannot sign in as the owner again. Your records stay in `foundry.db`, but there is no command that makes a new owner key for them. Foundry still starts and tells you where it expected the key file.

## Where to go next

- [The walkthrough](WALKTHROUGH.md): from a concept to a first sale, step by step.
- [Security and deployment](../SECURITY.md): what is protected, and what is your job.
- [AI setup](AI.md): connect an AI assistant or the optional AI review.
- [Architecture](ARCHITECTURE.md): how records, evidence and steps fit together.
- [The README](../README.md): the full list of commands.
