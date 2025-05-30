from sklearn.metrics.pairwise import cosine_similarity
import numpy as np
from tqdm import tqdm
import random

def average_with_harmonic_series(arr, axis=0):
    weights = [0.0] * arr.shape[axis]
    for i in range(arr.shape[axis]):
        weights[i] = 1. / (i + 1)
    return np.average(arr, weights=weights, axis=axis)

def positive_rank(target_aspect, segment_embs, embed_func):
    # Query: We assume that the segment is relevant to the target_aspect's parent aspect, so we do not need to include this within the query
    keywords = [f"{keyword} with respect to {str(target_aspect.get_ancestors(as_str=True))}" for keyword in target_aspect.keywords]
    keywords.insert(0, f"{target_aspect.name}: {target_aspect.description}")
    embed_res = embed_func(keywords).values()
    example_embed = random.choice(list(embed_res))  # pick an example embedding to determine the size of the embeddings
    keyword_embs = np.fromiter(embed_res, dtype=np.dtype((float, len(example_embed))))
    target_similarity = cosine_similarity(segment_embs, keyword_embs) # S x K

    # the more number of subaspects/keywords discussed, the better
    mean_pos = average_with_harmonic_series(target_similarity, axis=1).reshape((len(segment_embs), -1)) # S x 1
    return mean_pos

def negative_rank(neg_aspects, segment_embs, embed_func, breadth_weight=0.5):
    # Query: We assume that the segment is relevant to the neg_aspects' parent aspect, so we do not need to include this within the query
    # penalize both breadth of neg_aspects discussed (mean of mean) AND depth of neg_aspects discussed (max of mean overall)

    aspect_sims = np.zeros((len(neg_aspects), len(segment_embs)))
    for idx, aspect in tqdm(enumerate(neg_aspects), total=len(neg_aspects)):
        keywords = [f"{keyword} with respect to {aspect.name}" for keyword in aspect.keywords]
        keywords.insert(0, f"{aspect.name}: {aspect.description}")
        embed_res = embed_func(keywords).values()
        example_embed = random.choice(list(embed_res))  # pick an example embedding to determine the size of the embeddings
        keyword_embs = np.fromiter(embed_res, dtype=np.dtype((float, len(example_embed))))
        neg_aspect_sim = cosine_similarity(segment_embs, keyword_embs) # S x K
    
        # the more number of subaspects/keywords discussed, the worse -> 
        mean_neg = average_with_harmonic_series(neg_aspect_sim, axis=1) # S x 1
        aspect_sims[idx] = mean_neg
    
    breadth_rank = aspect_sims.mean(axis=0).reshape((len(segment_embs), -1))
    depth_rank = aspect_sims.max(axis=0).reshape((len(segment_embs), -1))
    
    neg_rank = (breadth_weight * breadth_rank) + ((1-breadth_weight) * depth_rank)
    return neg_rank

def discriminative_rank(target_aspect, neg_aspects, segment_embs, embed_func, beta, gamma):
    
    # Reward chunks that discuss the target aspect (S x 1)
    print(f"Computing positive rank for {target_aspect.name}")
    pos_r = positive_rank(target_aspect, segment_embs, embed_func)
    
    # Penalize chunks that discuss the distractor aspects (S x 1)
    print(f"Computing negative rank for {target_aspect.name}")
    neg_r = negative_rank(neg_aspects, segment_embs, embed_func)
    
    disc_rank = (pos_r * beta)/(neg_r * gamma)
    return disc_rank

def aspect_segment_ranking(args, segments, target_aspect, neg_aspects):
    """Step 3: Rank corpus segments based on relevance to keywords."""
    
    # we are by default, provided segments that are relevant to a given aspect
    # however, which are most likely to contain all relevant subaspects? we assume that the keywords cover many subaspects
    # we also want to penalize the segments which discuss a multitude of other aspects, given that they may distract during subaspect discovery + perspective

    segment_embs = list(args.embed_func(text=segments, model=args.embed_model).values())
    segment_embs = np.array(segment_embs)
    segment_scores = discriminative_rank(target_aspect, neg_aspects, segment_embs, lambda x: args.embed_func(x, model=args.embed_model), args.beta, args.gamma)
    segment_ranks = sorted(np.arange(len(segments)), key=lambda x: -segment_scores[x][0])

    id2score = {}
    rank2id = {}
    for rank, seg_id in enumerate(segment_ranks):
        id2score[seg_id] = segment_scores[seg_id]
        rank2id[rank] = seg_id
    
    return rank2id, id2score
    