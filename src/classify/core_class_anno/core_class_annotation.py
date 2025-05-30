import math
import torch
import json
import copy
from tqdm import tqdm
import mmap
import argparse
import queue

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity as cos

from vllm import SamplingParams
from vllm.sampling_params import GuidedDecodingParams
import json
from pydantic import BaseModel, conlist

class cls_schema(BaseModel):
    aspect_label: conlist(str, max_length=5)

def cls_prompt(instruction, doc, class_names):
    prompt = "You are a multi-label text classifier." + instruction + f"""
Your corpus_segment is below:
corpus_segment: {doc}

Your aspect_options are the following:
aspect_options: {str(class_names)}

Output your answer in the following JSON format:
{{
    aspect_label: <list of strings, where the string values are the names of the aspects from aspect_options that the corpus_segment discusses (with reasonable confidence); if no options are applicable, leave the list empty>
}}
"""
    return prompt

def api_call(args, doc, class_names, instruction, demos=[]):
    '''
    args
    doc str: query
    instruction str: system instruction
    demos List((str, str)): demonstrations, if any
    temperature: None for default temprature
    '''
    class_names = [item.lower() for item in class_names]
    
    if args.chat_model_name == "vllm":
        guided_decoding_params = GuidedDecodingParams(json=cls_schema.model_json_schema())
        sampling_params = SamplingParams(max_tokens=500, guided_decoding=guided_decoding_params, temperature=0.1, top_p=0.99)

        prompt = "You are a multi-label text classifier." + instruction + f"""
Your corpus_segment is below:
corpus_segment: {doc}

Your aspect_options are the following:
aspect_options: {str(class_names)}

Output your answer in the following JSON format:
{{
    aspect_label: <list of strings, where the string values are the names of the aspects from aspect_options that the corpus_segment discusses (with reasonable confidence); if no options are applicable, leave the list empty>
}}
        """
        
        output = args.chat_model.generate([prompt], sampling_params=sampling_params)[0].outputs[0].text
        aspects = json.loads(output)['aspect_label']
        temp_raw = ', '.join(aspects)
        
    elif (args.chat_model_name == "gpt-4o") or (args.chat_model_name == "gpt-4o-mini"):
        messages = [{"role": "system", "content": instruction}]
        
        for demo_doc, demo_label in demos[::-1]:
            messages.append({"role": "user", "content": demo_doc})
            messages.append({"role": "assistant", "content": demo_label})
        
        messages.append({"role": "user", "content": doc})
        
        assert len(messages) == 2
        input_text = messages[0]["content"] + messages[1]["content"]
        
        response = args.chat_model([input_text])[0]
        temp_raw = response.split(':')[-1].strip()
    else:
        raise ValueError(f'Invalid chat model name: {args.chat_model_name} not implemented yet.')

    return temp_raw

def get_num_lines(file_path):
    fp = open(file_path, "r+")
    buf = mmap.mmap(fp.fileno(), 0)
    lines = 0
    while buf.readline():
        lines += 1
    return lines

class Node:
    def __init__(self, node_id, name):
        self.name = name
        self.node_id = node_id
        self.parents = []
        self.childs = []
        self.similarity_score = 0
        self.path_score = 0

    def addChild(self, child):
        if child not in self.childs:
            self.childs.append(child)

    def addParent(self, parent):
        if parent not in self.parents:
            self.parents.append(parent)

    def findChild(self, node_id):
        if node_id == self.node_id:
            return self
        if len(self.childs) == 0:
            return None
        for child in self.childs:
            ans = child.findChild(node_id)
            if ans != None:
                return ans
        return None

def createGraph(label_path, label_hierarchy_path):
    root = None

    id2label = {}
    label2id = {}
    with open(label_path) as f:
        for line in f:
            label_id, label_name = line.strip().split('\t')
            id2label[label_id] = label_name
            label2id[label_name] = label_id

    # construct graph from file
    with open(label_hierarchy_path) as f:
        ## for each line in the file
        root = Node(-1, 'ROOT')
        for line in f:
            parent_id, child_id = line.strip().split('\t')
            parent = id2label[parent_id]
            child = id2label[child_id]
            parent_node = root.findChild(parent_id)
            if parent_node is None:
                parent_node = Node(parent_id, parent)
                root.addChild(parent_node)
                parent_node.addParent(root)
            child_node = root.findChild(child_id)
            if child_node is None:
                child_node = Node(child_id, child)
            parent_node.addChild(child_node)
            child_node.addParent(parent_node)
    
    return root, id2label, label2id


def bfs(root, doc_emb, key_term_emb_dict):
    queue = [root]  # current level queue
    next_queue = [] # next level queue  
    similarity_score_dic = {}
    level = 0       # current level
    while len(queue) > 0:
        ## for each class at level l in the queue
        for node in queue:
            ## calculate <document, class> similarity score for all its child
            childs = node.childs
            if len(childs) == 0:
                continue
            c2s = {}
            for cur_child in childs:
                cur_name = cur_child.name
                cur_child_embed = key_term_emb_dict[cur_name]
                scores = max(cos(doc_emb.reshape(1,-1), cur_child_embed).tolist()[0])
                c2s[cur_name]= math.exp(scores)

            for child in childs:
                child.similarity_score = c2s[child.name]
                cur_parents = child.parents
                cur_parents.sort(key=lambda x: x.path_score, reverse=True)
                child.path_score = child.similarity_score * cur_parents[0].path_score

            ## select l + 3 classes from its children classes that are most similar to D
            childs.sort(key=lambda x: x.similarity_score, reverse=True)
            next_queue.extend(childs[:min(len(childs), level + 3)])

        ## after processing all node at current level, proceed to next level
        if level == 0:
            queue = next_queue
        else:
            next_queue.sort(key=lambda x: x.path_score, reverse=True)
            queue = next_queue[:(level+2)**2]

        for child in queue:
            similarity_score_dic[child.node_id] = child.similarity_score

        next_queue = []
        level += 1
    # return a list of class names that are selected
    return similarity_score_dic

def process_document_batch(args, root_node, id2label, label2id, documents, gpt_template, total_doc_embedding, key_term_emb_dict):
    # make a tree copy
    root = copy.deepcopy(root_node)
    # set root path score to 1
    root.path_score = 1
    root.similarity_score = 1
    # process all level, calculate path score

    prompts = []
    for index, doc in enumerate(documents):
        doc_emb=total_doc_embedding[index]
        sim_dic = bfs(root, doc_emb, key_term_emb_dict)
        class_names = [id2label[cid].replace('_', ' ') for cid in sim_dic]
        instruction = gpt_template.format(', '.join(class_names))
        prompts.append(cls_prompt(instruction, doc, class_names))
    
    if args.chat_model_name == "vllm":
        guided_decoding_params = GuidedDecodingParams(json=cls_schema.model_json_schema())
        sampling_params = SamplingParams(max_tokens=500, guided_decoding=guided_decoding_params, temperature=0.1, top_p=0.99)

        outputs = args.chat_model.generate(prompts, sampling_params=sampling_params)
        
        doc_aspects = [json.loads(output.outputs[0].text)['aspect_label'] for output in outputs]
    elif (args.chat_model_name == "gpt-4o") or (args.chat_model_name == "gpt-4o-mini"):
        outputs = args.chat_model(prompts, temperature=0.1, top_p=0.99)
        outputs = [json.loads(o.split("```json")[1].split("```")[0].strip())['aspect_list']
                   if "```json" in o 
                   else json.loads(o.strip())['aspect_list']
                   for o in outputs]
    else:
        raise ValueError(f'Invalid chat model name: {args.chat_model_name} not implemented yet.')
    
    
    all_outputs = []
    for aspects in doc_aspects:
        class_names = [cn.replace(' ', '_') for cn in aspects]
        classes = [label2id[cn] for cn in class_names if cn in label2id]

        q = queue.Queue()
        class_set = set(classes)
        for c in classes:
            q.put(root.findChild(c))
            # print(c)
        while not q.empty():
            c = q.get()
            for p in c.parents:
                pid = p.node_id
                if pid == -1: continue
                q.put(p)
                class_set.add(pid)
        
        doc_output = {'response':str(aspects), 
                      'core classes':classes,
                      'with ancestors': list(class_set)}
        all_outputs.append(doc_output)
    
    return all_outputs

def process_document(args, root_node, id2label, label2id, document_text, gpt_template, doc_emb, key_term_emb_dict):
    
    # make a tree copy
    root = copy.deepcopy(root_node)
    # set root path score to 1
    root.path_score = 1
    root.similarity_score = 1
    # process all level, calculate path score
    sim_dic = bfs(root, doc_emb, key_term_emb_dict)

    class_names = [id2label[cid].replace('_', ' ') for cid in sim_dic]
    instruction = gpt_template.format(', '.join(class_names))
    response = api_call(args, document_text, class_names, instruction, demos=[])
    class_names = [cn.replace(' ', '_') for cn in response.split(', ')]
    classes = [label2id[cn] for cn in class_names if cn in label2id]

    q = queue.Queue()
    class_set = set(classes)
    for c in classes:
        q.put(root.findChild(c))
        # print(c)
    while not q.empty():
        c = q.get()
        for p in c.parents:
            pid = p.node_id
            if pid == -1: continue
            q.put(p)
            class_set.add(pid)

    return {'response':response, 
            'core classes':classes, 
            'with ancestors': list(class_set)}
    
def run_annotation(args, claim, llm_enrichment_path, corpus_path, label_path, label_hierarchy_path, output_path, gpu=0):
    
    """ load label-keyterm dict """
    enriched_file = llm_enrichment_path
    label_keyterm_dict = {}
    with open(enriched_file) as file:
        for line in file:
            components = line.strip().split(':')
            node = components[0]
            keywords = components[1]
            keyword_list = keywords.split(',')
            label_keyterm_dict[node] = keyword_list
    
    """ prompt init """
    gpt_template = f'You will be provided with a corpus segment. This segment is about a claim: {claim}.\n' +'Please select the suitable aspect it talks about from the following aspects: {}. Separate by comma if there are multiple. Just give the aspect names as shown in the provided list starting with aspect:.'

    """ model init """
    model_name = 'all-mpnet-base-v2'
    model = SentenceTransformer(model_name, device=f'cuda:{gpu}') 
    
    """ construct graph from file """
    root, id2label, label2id = createGraph(label_path, label_hierarchy_path)
    num_line = get_num_lines(corpus_path)
    num_class = len(id2label)
    writing_result = {}
    
    """ load corpus """
    all_docs = [] 
    all_docs_id = []
    with open(corpus_path) as f:
        for i, line in tqdm(enumerate(f), total=num_line):
            ## get current line and process document
            doc_id, doc = line.strip().split('\t')
            all_docs.append(doc)
            all_docs_id.append(doc_id)
    all_docs = all_docs
    all_docs_id = all_docs_id
    
    """  get embeddings for all documents and key terms """
    with torch.no_grad():
        total_doc_embedding = model.encode(all_docs, batch_size=128, 
                                        show_progress_bar=True, convert_to_numpy=True)
    
    key_term_emb_dict = {}
    for i in tqdm(range(num_class)):
        current_label = id2label[str(i)]
        current_key = [current_label]+label_keyterm_dict[current_label]
        current_key = [i.replace('_', ' ') for i in current_key]
        current_embed = model.encode(current_key, batch_size=128, convert_to_numpy=True)
        key_term_emb_dict[current_label] = current_embed

    """ BATCH processing of all documents """
    results = process_document_batch(args, root, id2label, label2id, all_docs, gpt_template, total_doc_embedding, key_term_emb_dict)

    for doc_id, result in zip(all_docs_id, results):
        writing_result[doc_id] = result
    
    json.dump(writing_result, open(output_path, 'w'), indent=1)


if __name__ == '__main__':

    """ args init """
    parser = argparse.ArgumentParser(description='main', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--llm_enrichment_path', type=str)
    parser.add_argument('--corpus_path', type=str)
    parser.add_argument('--label_path', type=str)
    parser.add_argument('--label_hierarchy_path', type=str)
    parser.add_argument('--output_path', type=str)
    parser.add_argument('--gpu', type=int, default=0)
    parser.add_argument('--claim', type=str, default='Pfizer COVID-19 vaccine is better than Moderna COVID-19 vaccine.')
    args = parser.parse_args()
    
    """ run annotation """
    run_annotation(args.claim, args.llm_enrichment_path, args.corpus_path, args.label_path, args.label_hierarchy_path, args.output_path, args.gpu)

