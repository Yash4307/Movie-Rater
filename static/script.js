document.addEventListener('DOMContentLoaded', function() {
    // --- 1. Theme Management ---
    const themeToggle = document.getElementById('themeToggle');
    const body = document.body;

    // Check for saved theme
    const savedTheme = localStorage.getItem('theme') || 'light-theme';
    body.className = savedTheme;
    updateThemeIcon(savedTheme);

    if (themeToggle) {
        themeToggle.addEventListener('click', () => {
            const isDark = body.classList.contains('dark-theme');
            const newTheme = isDark ? 'light-theme' : 'dark-theme';
            body.className = newTheme;
            localStorage.setItem('theme', newTheme);
            updateThemeIcon(newTheme);
        });
    }

    function updateThemeIcon(theme) {
        const icon = themeToggle?.querySelector('i');
        if (icon) {
            icon.className = theme === 'dark-theme' ? 'fas fa-sun' : 'fas fa-moon';
        }
    }

    // --- 2. Movie Search Logic ---
    const searchBtn = document.getElementById('searchBtn');
    const movieSearch = document.getElementById('movieSearch');
    const suggestionsBox = document.getElementById('searchSuggestions');
    const loadingSpinner = document.getElementById('loadingSpinner');
    const movieResult = document.getElementById('movieResult');

    if (searchBtn && movieSearch) {
        searchBtn.addEventListener('click', searchMovie);
        movieSearch.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') searchMovie();
        });
        movieSearch.addEventListener('input', debounce(handleLiveSearch, 250));
    }

    async function searchMovie() {
        const query = movieSearch.value.trim();
        if (!query) return showToast('Please enter a movie title', 'error');

        loadingSpinner.style.display = 'block';
        movieResult.style.display = 'none';

        try {
            const response = await fetch('/search_movie', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: query })
            });
            const data = await response.json();
            
            loadingSpinner.style.display = 'none';
            if (data.error) {
                showToast(data.error, 'error');
            } else {
                displayMovie(data);
            }
        } catch (error) {
            loadingSpinner.style.display = 'none';
            showToast('Search failed. Check your connection.', 'error');
        }
    }

    // --- 3. UI Display Logic ---
    function displayMovie(movie) {
        const posterUrl = movie.poster || 'https://via.placeholder.com/300x450?text=No+Poster';
        
        movieResult.innerHTML = `
            <div class="movie-header">
                <h2>${movie.title} (${movie.year})</h2>
            </div>
            <div class="movie-content">
                <div class="movie-poster">
                    <img src="${posterUrl}" id="currentPoster" alt="${movie.title}">
                </div>
                <div class="movie-details">
                    <div class="movie-info">
                        <p><strong><i class="fas fa-star" style="color: #f1c40f;"></i> Rating:</strong> ${movie.rating || 'N/A'}/10</p>
                        <p><strong><i class="fas fa-book"></i> Plot:</strong> ${movie.plot}</p>
                    </div>
                    
                    <div class="action-buttons" style="margin-top: 1rem; display: flex; gap: 10px;">
                        <button id="addWatchlist" class="btn-primary" style="background: var(--success-color);">
                            <i class="fas fa-plus"></i> Watchlist
                        </button>
                    </div>

                    <div class="rating-section" style="margin-top: 2rem; border-top: 1px solid var(--border-color); padding-top: 1rem;">
                        <h4>Rate this movie:</h4>
                        <div class="rating-stars" data-id="${movie.id}" data-title="${movie.title}">
                            ${[1, 2, 3, 4, 5].map(i => `<i class="fas fa-star" data-value="${i}"></i>`).join('')}
                        </div>
                        <textarea id="reviewText" placeholder="Write a short review..." rows="2"></textarea>
                        <button id="submitRating" class="btn-primary">Submit Review</button>
                    </div>
                </div>
            </div>
        `;

        movieResult.style.display = 'block';
        movieResult.scrollIntoView({ behavior: 'smooth' });
        setupInteractionLogic(movie);
    }

    // --- 4. Rating & Watchlist Interactions ---
    function setupInteractionLogic(movie) {
        const stars = document.querySelectorAll('.rating-stars i');
        const submitBtn = document.getElementById('submitRating');
        const watchlistBtn = document.getElementById('addWatchlist');
        let selectedRating = 0;

        // Star Hover & Click
        stars.forEach(star => {
            star.addEventListener('click', () => {
                selectedRating = star.dataset.value;
                stars.forEach(s => s.classList.toggle('active', s.dataset.value <= selectedRating));
            });
        });

        // Submit Rating
        submitBtn.addEventListener('click', async () => {
            if (selectedRating === 0) return showToast('Please select stars', 'error');
            
            const payload = {
                movie_id: movie.id,
                movie_title: movie.title,
                poster_url: movie.poster,
                genres: movie.genres || [],
                rating: selectedRating * 2, // Normalize to 10-point scale
                review: document.getElementById('reviewText').value
            };

            const res = await fetch('/rate_movie', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                showToast('Review saved!', 'success');
                setTimeout(() => location.reload(), 1500);
            }
        });

        // Add to Watchlist
        watchlistBtn.addEventListener('click', async () => {
            const res = await fetch('/add_to_watchlist', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    movie_id: movie.id,
                    movie_title: movie.title,
                    poster_url: movie.poster
                })
            });
            if (res.ok) {
                showToast('Added to Watchlist!', 'success');
                appendMovieToWatchlist(movie);
            }
        });
    }

    function appendMovieToWatchlist(movie) {
        const tab = document.getElementById('WatchlistTab');
        if (!tab) return;

        const emptyMsg = tab.querySelector('.empty-msg');
        if (emptyMsg) emptyMsg.remove();

        let grid = tab.querySelector('.ratings-grid');
        if (!grid) {
            grid = document.createElement('div');
            grid.className = 'ratings-grid';
            tab.appendChild(grid);
        }

        // Avoid duplicate cards on repeated clicks
        const exists = Array.from(grid.querySelectorAll('h4')).some(h => h.textContent === movie.title);
        if (exists) return;

        const card = document.createElement('div');
        card.className = 'rating-card watchlist-card';
        card.innerHTML = `
            ${movie.poster ? `<img src="${movie.poster}" alt="${movie.title}" class="card-poster">` : ''}
            <div class="card-info">
                <h4>${movie.title}</h4>
                <button class="btn-small rate-now" onclick="fillSearch('${movie.title.replace(/'/g, "\\'")}')">Rate Now</button>
            </div>
        `;
        grid.prepend(card);
    }

    // --- 5. Global Toast Utility ---
    function showToast(msg, type) {
        const container = document.querySelector('.toast-container');
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.innerHTML = `
            <div class="toast-body">${msg}</div>
            <button onclick="this.parentElement.remove()">&times;</button>
        `;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    async function handleLiveSearch() {
        const query = movieSearch.value.trim();
        if (!suggestionsBox) return;
        if (query.length < 2) {
            suggestionsBox.innerHTML = '';
            return;
        }
        try {
            const res = await fetch(`/search_suggestions?q=${encodeURIComponent(query)}`);
            const data = await res.json();
            if (!Array.isArray(data) || data.length === 0) {
                suggestionsBox.innerHTML = '';
                return;
            }
            suggestionsBox.innerHTML = data.map(item => `
                <button class="suggestion-item" type="button" data-title="${item.title}">
                    ${item.title} <small>(${item.year || 'N/A'})</small>
                </button>
            `).join('');
            suggestionsBox.querySelectorAll('.suggestion-item').forEach(btn => {
                btn.addEventListener('click', () => {
                    movieSearch.value = btn.dataset.title;
                    suggestionsBox.innerHTML = '';
                    searchMovie();
                });
            });
        } catch (e) {
            suggestionsBox.innerHTML = '';
        }
    }

    function debounce(fn, delay) {
        let timer;
        return (...args) => {
            clearTimeout(timer);
            timer = setTimeout(() => fn(...args), delay);
        };
    }

    document.querySelectorAll('.like-btn').forEach(btn => {
        btn.addEventListener('click', async () => {
            const ratingId = btn.dataset.ratingId;
            const res = await fetch(`/like_review/${ratingId}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok || data.error) {
                showToast(data.error || 'Could not update like', 'error');
                return;
            }
            btn.classList.toggle('liked', data.liked);
            const count = btn.querySelector('.like-count');
            if (count) count.textContent = data.count;
        });
    });
});
