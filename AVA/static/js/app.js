const playButton = document.getElementById("play-script");
const generatedScript = document.getElementById("generated-script");

if (playButton && generatedScript) {
	const scriptText = generatedScript.textContent.trim();

	if (!scriptText || !("speechSynthesis" in window)) {
		playButton.disabled = true;
	} else {
		playButton.addEventListener("click", () => {
			if (window.speechSynthesis.speaking || window.speechSynthesis.pending) {
				window.speechSynthesis.cancel();
				playButton.textContent = "▶ Play";
				return;
			}

			const utterance = new SpeechSynthesisUtterance(scriptText);
			utterance.addEventListener("end", () => {
				playButton.textContent = "▶ Play";
			});
			utterance.addEventListener("error", () => {
				playButton.textContent = "▶ Play";
			});

			playButton.textContent = "■ Stop";
			window.speechSynthesis.speak(utterance);
		});
	}
}
